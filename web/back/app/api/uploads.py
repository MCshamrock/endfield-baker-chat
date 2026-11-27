"""api/uploads.py —— 取件码：免登录上传数据包，之后凭码把数据领走。

## 两组接口，同一张表

新接口（App 内用）：

    POST /v1/uploads           multipart `file` → 201 {code, expires_at, bytes, source}
    GET  /v1/uploads/{code}    → {code, bytes, source, expires_at, claimed, claim_count}
    POST /v1/uploads/claim     要登录（Bearer）→ 导入结果（和 /v1/imports 同形状）

老接口（线上 3.2.0–3.2.2 的老客户端用，**形状一个字段都不能变**）：

    POST /upload               请求体 = 导出包原文（JSON，不是 multipart）
                               → {code, bytes, raw_bytes, uploaded_at, cleanup_at, keep_hours}
    POST /fetch                {"code": "..."} → 导出包原文（逐字节原样，不是包一层 JSON）

老接口为什么要存在：老客户端正在线上跑「云同步」。把域名指到新后端之后，
它们一行不改也得能继续传、继续取 —— 所以这两条只做"存/取"，不做导入
（老客户端自己解析包），但**共用同一张 upload_tickets 表和同一批文件**。

## 四个共同约定

1. **码不是一次性的**：24 小时内同一个码可以被多台设备、多个账号分别领。
   同一台设备重复领 → 导入幂等生效 → `duplicate: true`，这不算错误。
2. **限流按 IP**：`request.client.host`。真实部署在反代后面时，要让 uvicorn 开
   `--proxy-headers`（否则所有请求的 IP 都是 127.0.0.1，等于全站共用一个桶）。
3. **上传先顺手清一批过期包**（最多 20 条），否则磁盘只涨不落。
4. **老接口的额度更宽松**：新接口每 IP 每小时 5 个包，老接口 30 个 ——
   老客户端会重试、会一次补传几份，用新接口的额度卡它等于让老用户传不上去。
"""

import hashlib
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import errors, ratelimit, uploads

# 复用 /v1/imports 的"批次 → 对外形状"映射。它在下划线开头，本来是
# api/imports.py 内部用的；这里**不复制一份**的理由是：形状变一次要改两处，
# 迟早会漏（客户端拿到的两个入口必须是同一个形状）。那个文件这一轮不让动，
# 所以只能这样借。
from app.api.imports import _to_out as _import_to_out
from app.clock import to_iso_str
from app.deps import CurrentIdentity, DbSession
from app.models import UploadTicket
from app.repos import uploads as uploads_repo
from app.schemas import ImportResultOut
from app.settings import get_settings

logger = logging.getLogger("baker.uploads.api")

# /v1 下的新接口
router = APIRouter(tags=["uploads"])

# 根路径下的老接口（形状照老客户端，不能改）
alias_router = APIRouter(tags=["uploads-legacy"])

# 每次从上传流里读多少字节。和 api/imports.py 同一个值：
# 1MB 是"内存峰值"和"系统调用次数"的折中。
READ_CHUNK = 1024 * 1024

# 老接口的时间格式：`YYYY-MM-DD HH:MM:SS`（不带 T、不带时区）。
_LEGACY_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 老接口显示的是**服务器本地时间（CST，UTC+8）**，已对着线上实测核过。
# 用固定 +8 偏移而不是 zoneinfo/Asia/Shanghai：CST 没有夏令时，
# 固定偏移就够了，还省掉"服务器没装 tzdata 就崩"这一整类问题。
_LEGACY_TZ = timezone(timedelta(hours=8))


class LegacyError(errors.AppError):
    """老接口（`/upload`、`/fetch`）的错误：对外形状是 `{"ok": false, "error": "中文"}`。

    为什么必须单独一个类：老客户端对**没专门映射**的错误会把响应体前 120 字
    直接显示在界面上（`cloudData.ts:184`）。给它看新接口那套
    `{"error": {"code": …, "message": …}, "request_id": …}`，用户看到的就是一坨 JSON。
    所以这两个端点的错误走另一条出口（见 `main.py` 里的 handler）。
    """


# ---------------------------------------------------------------- 出参形状
class UploadTicketOut(BaseModel):
    """`POST /v1/uploads` 的返回。用户要把 `code` 抄下来（或截图），没有第二次机会。"""

    code: str
    expires_at: str
    bytes: int
    source: str


class UploadTicketInfoOut(BaseModel):
    """`GET /v1/uploads/{code}` 的返回：只给元信息，**不给内容**。

    `claimed` 是"有没有人领过"（`claim_count > 0`），前端用它提示
    "这个码已经被用过了，但还没过期、还能用"。
    """

    code: str
    bytes: int
    source: str
    expires_at: str
    claimed: bool
    claim_count: int


class ClaimRequest(BaseModel):
    """`POST /v1/uploads/claim` 的请求体。码允许小写/带空格，服务端会归一化。"""

    code: str = Field(min_length=1, max_length=32)


class LegacyUploadOut(BaseModel):
    """老接口 `/upload` 的返回。字段名、类型都对老客户端保持原样。

    老客户端里是这么用的（少一个字段就会显示 undefined 或直接抛错）：
      * `code`        必填，缺了它客户端抛「服务器未返回取件码」
      * `bytes` / `raw_bytes`  两个都在读，都是数字
      * `uploaded_at` / `cleanup_at`  字符串，`YYYY-MM-DD HH:MM:SS`
      * `keep_hours`  数字，客户端拿它算"还有多久过期"

    `bytes` 与 `raw_bytes`：我们现在只存原始字节，所以两个数一样。
    字段留着是因为客户端两个都读 —— 去掉任何一个都可能让它当成缺字段。

    `ok`：老服务成功时也带这个字段（`{"ok":true,...}`），对拍脚本按它判成功。
    我们多给一个字段不影响老客户端，但少了会让对拍/运维脚本判成"失败"。
    """

    ok: bool = True
    code: str
    bytes: int
    raw_bytes: int
    uploaded_at: str
    cleanup_at: str
    keep_hours: int


class LegacyFetchRequest(BaseModel):
    """老接口 `/fetch` 的请求体。

    刻意**不加最小长度校验**：老客户端如果传了个空码/缺字段，pydantic 会回 422 +
    新形状的错误体，而老客户端只认 `{"ok": false, "error": "中文"}`。
    这里让空码一路走到业务层，统一按"没这个码"回 404（老形状）。
    """

    code: str = Field(default="", max_length=64)


# ---------------------------------------------------------------- 新接口
@router.post("/uploads", response_model=UploadTicketOut, status_code=201)
async def create_upload(
    file: UploadFile, db: DbSession, request: Request
) -> UploadTicketOut:
    """免登录传一个数据包，换一个 8 位取件码。

    这一步**不导入**：传的人还没登录，数据不知道该归谁。真正的导入发生在
    注册（带 `claim_code`）或登录后调 `/v1/uploads/claim` 的时候。
    """
    settings = get_settings()
    _throttle(request)
    # 先清一小批过期的，再算总量 —— 否则"过期但没清"的包会把 2GB 额度一直占着
    uploads.lazy_cleanup(db)
    data = await _read_limited(file, settings.import_max_bytes)
    ticket = uploads.create_ticket(db, data=data, filename=file.filename or "")
    uploads.commit_or_discard(db, ticket)
    logger.info(
        "取件码 %s 已发出（%s，%d bytes）",
        uploads.code_hint(ticket.code),
        ticket.source,
        ticket.bytes,
    )
    return _ticket_out(ticket)


@router.get("/uploads/{code}", response_model=UploadTicketInfoOut)
def get_upload(code: str, db: DbSession) -> UploadTicketInfoOut:
    """只看这个码对不对、什么时候过期、有没有人领过。不回内容。

    为什么单独给这个接口：前端让用户"先填码确认一下"再走注册，比注册完才发现
    码抄错了体验好得多（那时候账号已经建好了）。
    """
    ticket = uploads.get_ticket(db, code)
    return UploadTicketInfoOut(
        code=ticket.code,
        bytes=ticket.bytes,
        source=ticket.source,
        expires_at=to_iso_str(ticket.expires_at),
        claimed=ticket.claim_count > 0,
        claim_count=ticket.claim_count,
    )


@router.post("/uploads/claim", response_model=ImportResultOut)
def claim_upload(
    req: ClaimRequest, db: DbSession, identity: CurrentIdentity
) -> ImportResultOut:
    """凭码把包导进当前设备/账号。

    为什么要求登录：导入要挂到某个人名下。免登录领 = 数据落在匿名设备上，
    换台手机就找不回来了；而且匿名领取等于给了"拿码撞库"的动机。
    返回和 `POST /v1/imports` 完全一样的形状，客户端不用写两套展示。
    """
    user = identity.user
    if user is None:
        raise errors.no_user()

    batch, duplicate = uploads.claim(
        db, code=req.code, device=identity.device, user_id=user.id
    )
    db.commit()
    return _import_to_out(batch, duplicate=duplicate)


# ---------------------------------------------------------------- 老接口别名
@alias_router.post("/upload", response_model=LegacyUploadOut)
async def legacy_upload(db: DbSession, request: Request) -> LegacyUploadOut:
    """老客户端的上传接口：请求体就是导出包原文（JSON），不是 multipart。

    三条刻意的宽松：
      * **不校验包结构**。这批客户端我们改不了；它传上来的包只要不是空的就先收下，
        校验交给它自己解析的时候。服务端在这一步挑刺，只会让"云同步"莫名断掉。
      * **不要登录**。老形状里没有账号/设备概念，码本身就是凭证。
      * **额度更宽**（每 IP 每小时 30 个包，新接口是 5）。

    实现上有一点和 `/v1/uploads` 不同：body 是**分块写进临时文件**再改名的，
    不读进内存 —— 老客户端的包有 60MB，线上旧服务峰值内存约 130MB，
    我们这里峰值只有一个读块（1MB）。

    所有错误都抛 `LegacyError`：老客户端只认 `{"ok": false, "error": "中文"}`。
    """
    settings = get_settings()
    temp_path: Path | None = None
    try:
        _legacy_throttle(request)
        _reject_by_content_length(request, settings.import_max_bytes)
        # 先粗查一次空间：已经满了就别让用户白传 64MB
        uploads.check_capacity(db, 0)
        uploads.lazy_cleanup(db)

        temp_path, size, sha = await _stream_body_to_temp(
            request, settings.import_max_bytes
        )
        if size == 0:
            # 线上老服务的文案就是这三个字，客户端会把它显示出来
            raise LegacyError("bad_request", "请求体为空", http_status=400)

        ticket = uploads.create_ticket_from_temp(
            db, temp_path=temp_path, size=size, sha256=sha
        )
        uploads.commit_or_discard(db, ticket)
    except LegacyError:
        # 已经是老形状了，原样往外抛（别再包一层，否则 cause 指向自己）
        raise
    except errors.AppError as exc:
        raise _as_legacy(exc) from exc
    except Exception as exc:  # noqa: BLE001 —— 兜底也要是老形状，别让用户看到一坨 JSON
        logger.exception("老接口 /upload 失败")
        raise LegacyError("internal_error", "服务器出了点问题，请稍后再试", 500) from exc
    finally:
        # 正常路径上临时文件已经被 rename 走了；这里兜底"库里插失败/提交失败"
        if temp_path is not None:
            uploads_repo.remove_path(temp_path)

    logger.info(
        "老接口 /upload 收下 %d bytes，发出取件码 %s",
        ticket.bytes,
        uploads.code_hint(ticket.code),
    )
    return LegacyUploadOut(
        code=ticket.code,
        bytes=ticket.bytes,
        raw_bytes=ticket.bytes,
        uploaded_at=_legacy_time(ticket.created_at),
        # cleanup_at = 这个包的下次清理时刻**就是上传时刻 + 24h**。
        # 线上旧服务这里是"下一个 12:00 检查点"，我们不做那个语义：
        # 界面上写着"保留 24 小时"，那就应该真的是上传后 24 小时。
        cleanup_at=_legacy_time(ticket.expires_at),
        keep_hours=settings.upload_ttl_hours,
    )


@alias_router.post("/fetch")
def legacy_fetch(req: LegacyFetchRequest, db: DbSession, request: Request) -> Response:
    """老客户端的取件接口：给码，把包里那串字节**原样**还回去。

    **不做导入**：老客户端拿到包之后自己解析，服务端只管把字节送到。
    这里的"领取"只是计数（前端能看到"这个码被取过几次"）。
    也不 re-serialize、不加 BOM —— 客户端会直接 `JSON.parse`，多一个字节都可能失败。

    返回头：`Content-Type: application/json; charset=utf-8`，**不加 Content-Encoding**
    （没有 gzip：包本来就是文本，客户端也不解压；加了反而让它拿到乱码）。
    """
    try:
        _fetch_throttle(request)
        if not req.code.strip():
            # 老服务对"没给码"是 400 +「缺少取件码」；回 404 会让客户端以为"码已过期"，
            # 界面上是两种提示：一个让重填，一个让回原设备重传。
            raise LegacyError("bad_request", "缺少取件码", http_status=400)
        ticket, data = uploads.fetch_payload(db, req.code)
        db.commit()
    except LegacyError:
        raise
    except errors.AppError as exc:
        # 老客户端只认 404（"这个码没有/已经清理了"），不看错误体里的 code。
        # 文案照抄线上：多一个字少一个字都可能让用户看不懂该重传还是该等。
        if isinstance(exc, uploads.ClaimCodeError):
            raise LegacyError(
                "no_claim_code",
                "取件码不存在或数据已被清理（保留 24~48 小时）",
                http_status=404,
            ) from exc
        raise _as_legacy(exc) from exc
    except Exception as exc:  # noqa: BLE001 —— 兜底也要是老形状
        logger.exception("老接口 /fetch 失败")
        raise LegacyError("internal_error", "服务器出了点问题，请稍后再试", 500) from exc

    logger.info(
        "老接口 /fetch 取走取件码 %s（%d bytes）",
        uploads.code_hint(ticket.code),
        len(data),
    )
    # 包是什么类型就按什么类型回：老客户端按 content-type 决定自己怎么解析。
    # JSON 带上 charset：老客户端在部分 WebView 里会按 latin-1 猜，中文会乱码。
    media_type = (
        "application/zip" if ticket.source == "zip" else "application/json; charset=utf-8"
    )
    return Response(content=data, media_type=media_type)


# ---------------------------------------------------------------- 内部
def _ticket_out(ticket: UploadTicket) -> UploadTicketOut:
    return UploadTicketOut(
        code=ticket.code,
        expires_at=to_iso_str(ticket.expires_at),
        bytes=ticket.bytes,
        source=ticket.source,
    )


def _legacy_time(value: datetime) -> str:
    """老接口的时间字符串：**服务器本地时间（CST，UTC+8）、不带时区**。

    已对着线上实测核过：线上给的就是 `YYYY-MM-DD HH:MM:SS` 的东八区时间。
    老客户端把这两个字段原样显示给用户，所以这里给 UTC 会整整差 8 小时。

    库里的时间统一是 naive UTC（见 `app/clock.py`），所以先贴上 UTC 再转 +8；
    直接 `astimezone` 会按"系统本地时区"解释那个 naive 值，机器时区一变就错。
    """
    return value.replace(tzinfo=UTC).astimezone(_LEGACY_TZ).strftime(_LEGACY_TIME_FORMAT)


def _as_legacy(exc: errors.AppError) -> LegacyError:
    """把业务层的新形状错误换成老接口的形状（文案、状态码都不动，只换外壳）。"""
    if isinstance(exc, LegacyError):
        return exc
    return LegacyError(exc.code, exc.message, http_status=exc.http_status)


def _throttle(request: Request) -> None:
    """新接口：每个 IP 每小时最多传几个包（默认 5）。

    为什么按小时：一个包几十兆，正常人一次只传一个；按分钟限额挡不住
    "每分钟一个、传一整天"，而磁盘增长看的正是小时级的量。
    """
    host = _client_host(request)
    allowed, retry = ratelimit.uploads_by_ip().allow(host)
    if not allowed:
        raise errors.AppError(
            "too_many_uploads",
            f"上传太频繁了：每个 IP 每小时最多 {get_settings().upload_rpm} 个包，"
            f"{max(1, round(retry))} 秒后再试",
            http_status=429,
            details={"retry_after": round(retry, 1)},
        )


def _legacy_throttle(request: Request) -> None:
    """老接口单独的桶：默认 30 个/小时。

    为什么单独一个、而且更宽：老客户端会重试、也可能一次补传好几份；
    用新接口的 5 个/小时卡它，等于让老用户突然传不上去（这就是兼容事故）。
    文案照抄线上：老客户端会把这句直接显示给用户。
    """
    host = _client_host(request)
    allowed, _retry = ratelimit.legacy_uploads_by_ip().allow(host)
    if not allowed:
        raise LegacyError(
            "too_many_uploads", "请求太频繁，请稍后再试", http_status=429
        )


def _fetch_throttle(request: Request) -> None:
    """取件单独限流：老客户端可能连续重试，但也不该让一个脚本把包拖走几百次。"""
    host = _client_host(request)
    allowed, _retry = ratelimit.fetch_by_ip().allow(host)
    if not allowed:
        raise LegacyError(
            "too_many_fetches", "请求太频繁，请稍后再试", http_status=429
        )


def _client_host(request: Request) -> str:
    """客户端 IP（限流键）。

    `request.client` 在 ASGI 直连测试里也可能为空（没经过 socket），
    空的时候用一个固定桶，总比拼出 None 崩掉强。
    """
    if request.client is None:
        return "unknown"
    return request.client.host


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    """分块读 multipart 上传内容，边读边数，超上限立刻停。

    为什么不用 `await file.read()` 一次读完：一个 200MB 的"包"会直接把进程内存
    吃满（单进程服务，没有别的 worker 帮它扛）。
    """
    buffer = bytearray()
    while True:
        chunk = await file.read(READ_CHUNK)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            # 新接口沿用新形状；老接口那边有自己的 `_legacy_too_large`
            raise errors.AppError(
                "too_large",
                f"数据包太大了（上限 {limit // (1024 * 1024)} MB）。"
                "老客户端导出时会自动控制体积，超过这个数说明文件不对",
                http_status=413,
            )
    return bytes(buffer)


async def _stream_body_to_temp(request: Request, limit: int) -> tuple[Path, int, str]:
    """把请求体分块写进临时文件，边写边算大小和 sha256。

    返回 (临时文件, 字节数, sha256)。临时文件放在 `upload_dir/tickets/` 里 ——
    必须和最终文件同一块盘，最后那一步 `Path.replace` 才是原子改名（而不是复制）。

    上限判定用的是 `>`（等于 67,108,864 字节**放行**），和线上一致：
    线上客户端会自己卡在 64MB，服务端比它严反而会拒掉本来能成功的包。
    """
    directory = uploads_repo.path_dir(get_settings().upload_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handle_fd, name = tempfile.mkstemp(
        prefix=".upload-", suffix=uploads.PARTIAL_SUFFIX, dir=str(directory)
    )
    path = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise _legacy_too_large(size, limit)
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:  # noqa: BLE001 —— 超限/断线/被取消都算，半个包一律删
        uploads_repo.remove_path(path)
        raise
    return path, size, digest.hexdigest()


def _reject_by_content_length(request: Request, limit: int) -> None:
    """先看 Content-Length，超了就不读 body 直接 413（省一次 60MB 的内存搬运）。

    判定同样是 `>`：正好等于上限的包要放行（线上就是放行的）。
    """
    raw = request.headers.get("content-length", "")
    if raw.isdigit() and int(raw) > limit:
        raise _legacy_too_large(int(raw), limit)


def _legacy_too_large(size: int, limit: int) -> LegacyError:
    """413 文案照抄线上：`数据过大（64.0MB > 64MB）`。

    老客户端对 413 有专门映射（"超过 64MB 上限"），但有些版本会把 body 的
    前 120 字也带上，所以这里给的是完整的中文句子 + 实际大小。
    """
    return LegacyError(
        "too_large",
        f"数据过大（{size / (1024 * 1024):.1f}MB > {limit / (1024 * 1024):g}MB）",
        http_status=413,
    )


__all__ = ["LegacyError", "alias_router", "router"]
