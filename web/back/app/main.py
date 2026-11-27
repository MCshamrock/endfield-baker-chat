"""main.py —— 装配层：把中间件、异常处理、路由拼成一个服务。

这个文件应该**只做装配**，不做业务。所以它只有五件事：
  1. 日志
  2. CORS（让网页版 / 安卓 APK 能跨域打这个后端）
  3. request_id（每个请求一个 id，串起日志和错误体）
  4. 异常 → 统一 JSON（全站唯一的出口，定义在 errors.py）
  5. /health、/readyz 两个探针，然后挂上 /v1 的路由

启动：
    uvicorn app.main:app --port 5660
"""

import logging
import os
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import errors
from app.api import (
    admin_router,
    attachments_router,
    auth_router,
    characters_router,
    conversations_router,
    data_router,
    imports_router,
    legacy_group_router,
    legacy_router,
    memories_router,
    meta_router,
    rag_compat_router,
    uploads_alias_router,
    uploads_router,
)
from app.api.uploads import LegacyError
from app.db import SessionLocal
from app.settings import Settings, get_settings

logger = logging.getLogger("baker")

# 请求头里回传这个 id，用户报错时让他抄出来，就能在日志里定位到那一次请求
REQUEST_ID_HEADER = "X-Request-Id"


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _install_temp_dir(settings)
    _install_log_redaction()

    app = FastAPI(
        title="BakerAI 后端",
        version="0.1.0",
        # 生产环境关掉 /docs 也行；本地留着方便直接用浏览器点接口
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    _install_request_id(app)
    _install_cors(app)
    _install_error_handlers(app)
    _install_probes(app)
    _install_routers(app)
    return app


# ---------------------------------------------------------------- 中间件
def _install_temp_dir(settings: Settings) -> None:
    """把 Python 的临时目录钉在项目里。

    为什么必须在启动时改：`UploadFile` 超过 1MB 会自动落到磁盘，而它问的是
    `tempfile.gettempdir()`。不改的话文件会跑到 `%TEMP%`（Windows）或 `/tmp`（Linux），
    违反"所有产物都在 BakerAI_new 里"的硬规矩 —— 而且删项目时那堆残留没人知道该不该删。
    """
    path = Path(settings.tmp_dir)
    path.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(path)
    # 有些库不走 tempfile 而是直接读环境变量（例如 uvicorn 的 multipart 解析）
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = str(path)


def _install_request_id(app: FastAPI) -> None:
    """给每个请求打一个 id，写进 request.state、响应头和每条日志。

    为什么自建而不是引第三方中间件：就十几行，逻辑一眼能看完；
    引一个库反而要读它的源码才知道 id 到底什么时候生成。
    """

    @app.middleware("http")
    async def add_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex  # 32 位，正好对上 usage_events.request_id 的字段宽度
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "%s %s -> %s req=%s",
            request.method,
            _path_for_log(request.url.path),
            response.status_code,
            request_id,
        )
        return response


# `GET /v1/uploads/{code}` 的码在路径里。访问日志谁都能看，而码就是取数据的凭证，
# 所以日志里只留前 2 位（`/v1/uploads/YD***`）。真正的码只在响应体里出现。
_UPLOAD_CODE_PATH = re.compile(r"^(/v1/uploads/)([A-Za-z0-9]{1,16})$")
_UPLOAD_CODE_IN_TEXT = re.compile(r"(/v1/uploads/)([A-Za-z0-9]{1,16})")


def _path_for_log(path: str) -> str:
    """把路径里的取件码打码，别的路径原样返回。

    为什么只处理这一条路径：全站只有这一个端点把凭证放在 URL 里
    （`/v1/uploads/claim` 的码在 body 里，日志看不到）。
    uvicorn 自己的访问日志也会打 URL，那条由 `_UploadCodeRedactor` 兜。
    """
    match = _UPLOAD_CODE_PATH.match(path)
    if match is None:
        return path
    return f"{match.group(1)}{match.group(2)[:2]}***"


def _mask_codes(text: str) -> str:
    """把一段文本里 `/v1/uploads/<code>` 的码打码。"""
    return _UPLOAD_CODE_IN_TEXT.sub(
        lambda match: f"{match.group(1)}{match.group(2)[:2]}***", text
    )


class _UploadCodeRedactor(logging.Filter):
    """日志过滤器：把任何日志里的取件码打码。

    为什么要在日志出口再拦一道：`_path_for_log` 只改了**我们自己**那条访问日志，
    但 uvicorn 的访问日志（`INFO: 127.0.0.1 - "GET /v1/uploads/AB3D7K9Q 200"`）
    是 uvicorn 自己打的，还会带上完整 URL。而码就是取数据的凭证 ——
    日志谁能看？同事、日志平台、运维。一条完整码就等于一个能拖走别人数据包的地址。

    uvicorn 的访问日志把 URL 放在 `record.args[2]`；其它日志放在格式化后的消息里。
    两种都处理，匹配不到就不动。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            masked = _mask_codes(args[2])
            if masked != args[2]:
                record.args = (args[0], args[1], masked, *args[3:])
        elif isinstance(record.msg, str):
            record.msg = _mask_codes(record.msg)
        return True


def _install_log_redaction() -> None:
    """给日志装"取件码打码"过滤器。

    装在哪、为什么要装两处（logging 的规则决定的，不是随手写的）：

    * **日志记录只在"创建它的那个 logger"上过一遍 `filters`**，祖先 logger 的
      filters 根本不参与 —— 所以配置里得有三份：
        - `uvicorn.access` / `uvicorn.error`：它们配了 `propagate=False`，
          记录只走自己的 handler；带完整 URL 的访问日志就是 `uvicorn.access` 打的。
        - 根 logger：`logging.info(...)` 这种直接打到根上的记录。
    * 根 logger 的 **handler** 上再加一份：所有冒泡上来的记录都会经过 handler，
      这一份是"万一以后谁新加了个 logger"的兜底。

    重复调用 `create_app()`（测试就是这样）时不能重复加，否则过滤器越叠越多。
    """

    def _attach(holder: logging.Filterer) -> None:
        if any(isinstance(item, _UploadCodeRedactor) for item in holder.filters):
            return
        holder.addFilter(_UploadCodeRedactor())

    _attach(logging.getLogger())
    _attach(logging.getLogger("uvicorn.access"))
    _attach(logging.getLogger("uvicorn.error"))
    for handler in logging.getLogger().handlers:
        _attach(handler)


def _install_cors(app: FastAPI) -> None:
    """允许网页版和安卓 APK 跨域打这个后端（预检 OPTIONS 由这一层直接回 200）。

    为什么必须装：APK 里的 WebView origin 是 `https://localhost`，网页版是另一个域，
    两者打这个后端都算跨域。浏览器会先发一个 `OPTIONS` 预检；没有这层的话
    FastAPI 找不到 `OPTIONS /chat`，直接回 405 —— 表现是"客户端一发请求就失败"，
    而日志里只有一条 405，很难联想到是 CORS。

    为什么 `allow_origins` 是 `*`：客户端的 origin 不固定（APK / EXE / 网页 / 自己搭的
    反代都可能有），写白名单等于每加一个客户端就改一次服务端。这不危险，因为
    鉴权用的是 `Authorization: Bearer`，不是 cookie —— 别的站点拿不到用户的 token。

    代价是 `allow_credentials` **必须**是 False：按 CORS 规范，带 cookie 的跨域请求
    不允许配 `*`，真要开 credentials 浏览器会把整条响应丢掉。我们不用 cookie，
    所以这个限制对我们没有影响。
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
        # 把 request_id 暴露给浏览器：出错时用户抄这个 id 过来，就能直接定位日志
        expose_headers=[REQUEST_ID_HEADER],
    )


# ---------------------------------------------------------------- 异常出口
def _install_error_handlers(app: FastAPI) -> None:
    """全站唯一的错误出口。业务代码只管抛，这里转 JSON。"""

    @app.exception_handler(errors.AppError)
    async def handle_app_error(request: Request, exc: errors.AppError) -> JSONResponse:
        return _error_response(
            request,
            status=exc.http_status,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 把 pydantic 的字段错误压成一句人话，别把内部结构直接丢给客户端
        details = [
            {"field": ".".join(str(part) for part in item["loc"][1:]), "reason": item["msg"]}
            for item in exc.errors()
        ]
        return _error_response(
            request,
            status=400,
            code="bad_request",
            message="请求参数不合法",
            details={"fields": details},
        )

    @app.exception_handler(LegacyError)
    async def handle_legacy_error(request: Request, exc: LegacyError) -> JSONResponse:
        """老接口（/upload、/fetch）的错误出口：`{"ok": false, "error": "中文"}`。

        为什么单独一个形状：老客户端对没专门映射的错误，会把响应体前 120 字
        直接贴在界面上。给它看新接口那套 `{"error": {...}, "request_id": ...}`，
        用户看到的就是一坨 JSON —— 这是兼容 bug，不是审美问题。
        状态码沿用（老客户端按 413/404/429 做专门提示）。
        """
        return _legacy_error_response(request, exc)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 未预料的异常：日志里留完整堆栈，给客户端的只有一句话（不泄露内部实现）
        logger.exception(
            "未处理异常 req=%s", getattr(request.state, "request_id", "-"), exc_info=exc
        )
        return _error_response(
            request,
            status=500,
            code="internal_error",
            message="服务端出了点问题，请稍后重试",
        )


def _legacy_error_response(request: Request, exc: LegacyError) -> JSONResponse:
    """老接口的错误体。**只有** `ok` 和一段中文：老客户端就认这两个键。

    刻意不带 `request_id`：老客户端会把整段 body 前半截显示出来，
    多一个键就多一段它看不懂的东西。排障用日志里的 request_id。
    """
    logger.info(
        "老接口错误 req=%s status=%s code=%s",
        getattr(request.state, "request_id", "-"),
        exc.http_status,
        exc.code,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={"ok": False, "error": exc.message},
    )


def _error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    """所有错误都长这个样子。客户端只需要认一种结构。"""
    body: dict[str, object] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(
        status_code=status,
        content={
            "error": body,
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


# ---------------------------------------------------------------- 探针
def _install_probes(app: FastAPI) -> None:
    """两个探针，区别很重要：

    `/health` —— 进程活着就算过。给 systemd / 负载均衡看"要不要重启我"。
    `/readyz` —— 数据库通不通、迁移跑没跑。给部署脚本看"现在能不能接流量"。
    """

    @app.get("/health")
    def health() -> dict[str, object]:
        # ok=True 是为了老 RAG 客户端的探活（它读的是 ok 字段）
        return {"status": "ok", "ok": True, "service": "baker-backend"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
                last_migration = db.scalar(text("SELECT MAX(version) FROM schema_versions"))
        except Exception as exc:  # noqa: BLE001 —— 这里就是要抓住一切，返回"没准备好"
            logger.warning("readyz 失败：%s", exc)
            return JSONResponse(status_code=503, content={"ok": False, "reason": str(exc)[:200]})
        # schema 是"契约版本"（给客户端判断的），migrations 是"最后一个迁移文件"（给运维看的）。
        # 两者不是一回事：改表不一定要改契约版本。
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "db": "mysql",
                "schema": get_settings().schema_version,
                "migrations": last_migration,
            },
        )


# ---------------------------------------------------------------- 路由
def _install_routers(app: FastAPI) -> None:
    """所有业务路由统一挂在 /v1 下。"""
    for router in (
        meta_router,
        auth_router,
        characters_router,
        conversations_router,
        # 设置里「危险操作」的服务端那一半（清空会话/消息/上下文）
        data_router,
        attachments_router,
        imports_router,
        uploads_router,
        memories_router,
        admin_router,
    ):
        app.include_router(router, prefix="/v1")
    # 老客户端兼容层挂在根路径（它请求的是 /chat、/chat/stream、/group/*）
    app.include_router(legacy_router)
    app.include_router(legacy_group_router)
    # 取件码的老形状别名也挂在根路径：/upload、/fetch（老客户端"云同步"用的）
    app.include_router(uploads_alias_router)
    # 老检索服务（rag.peilika.beer）的 /retrieve 也挂在根路径：改由算法检索回答
    app.include_router(rag_compat_router)


app = create_app()
