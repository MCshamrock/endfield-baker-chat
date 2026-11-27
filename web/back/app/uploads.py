"""uploads.py —— 取件码：先把数据包匿名传上来，之后凭 8 位码把数据领走。

## 为什么要有这一层

用户刚装好 APK、还没有账号，但手机里有一个几十兆的「导出数据包」。
如果一定要先注册再传，他要在最陌生的时刻做最麻烦的事（找文件、选文件、等上传）。
所以拆成两步：

    POST /v1/uploads   （免登录）→ 存包，发一个 8 位码
    注册时带 claim_code → 同一个事务里把包导进这个新账号

老客户端（线上 3.2.0–3.2.2 还在跑的那批）用的是另一对形状：`POST /upload` +
`POST /fetch`。它们和 /v1 的两个接口**共用同一张表和同一批文件**，只是进出口的
包装不一样 —— 见 `api/uploads.py` 里的兼容别名，以及 `docs/14-取件码上传.md`。

## 几条这里说了算的规则

1. **码不是一次性的**。24 小时内同一个码可以被多台设备、多个账号分别领取。
   可领条件只有"没过期"；同一台设备重复领，靠导入管线的 (device, sha256)
   幂等自然变成 `duplicate=true`，不额外报错。

2. **领取和导入在同一个事务里**。`claim()` 不 commit，由调用方提交；
   导入中途炸了就整体回滚 —— 码不能被"吃掉"（用户会以为数据丢了）。

3. **写盘和写库要么都成、要么都不成**。顺序是"先插库拿码 → 再把文件放到
   最终位置"；任何一步失败都把另一边撤掉（见 `_persist` 与 `commit_or_discard`）。

4. **清理是"过期即可删"**：默认 TTL 24 小时。每次上传顺手清 20 条
   （`lazy_cleanup`），全量清理交给 `scripts/cleanup_uploads.py`。
"""

import hashlib
import logging
import secrets
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import errors
from app.clock import utcnow
from app.imports import package as package_module
from app.imports import service as imports_service
from app.models import Device, ImportBatch, UploadTicket
from app.repos import uploads as uploads_repo
from app.settings import get_settings

logger = logging.getLogger("baker.uploads")

# 码的字符集：**和线上老服务逐字一致**（32 个字符，去掉 `0 O 1 I`）。
#
# 为什么 L 留着：老客户端对码做格式校验（`^[A-HJ-NP-Z2-9]{8}$`），它认 L。
# 我们如果为了"更好抄"把 L 也去掉，生成的码里有 L 时就轮到老客户端说"码格式不对"——
# 兼容优先，抄错的代价比"客户端直接拒绝"小得多。
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

# 一次上传请求最多顺带清几条。清太多会让上传变慢，而且清理本来就有专门的脚本。
LAZY_CLEANUP_LIMIT = 20

# 老接口分块落盘的临时文件后缀。清理任务靠它认出"上次进程被 kill 留下的半个包"。
PARTIAL_SUFFIX = ".part"


class ClaimCodeError(errors.AppError):
    """取件码本身不能用（不存在 / 过期 / 文件丢了 / 抢不到）。

    单独一个类是为了让调用方能把"码的问题"和其它业务错误分开：
    注册流程要把码的问题统一翻成 400 `bad_claim_code` 并回滚整次注册，
    但不能顺手把"包坏了"（bad_package）这类错误也一起改掉。
    """


# ---------------------------------------------------------------- 码
def new_code() -> str:
    """生成一个 8 位取件码。用 `secrets` 不用 `random`：码本身就是取数据的凭证。"""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str:
    """把用户输入的码归一化；不像码的返回空串（查不到 → 当作"没这个码"）。

    只做两件事：**去掉首尾空白 + 转大写**。用户会打成小写、会带空格（从消息里
    复制粘贴时最常见），这些不该让一个**对的码**被判错。

    ⚠️ 刻意**不做的**：把"非法字符"过滤掉、拿剩下的去查。线上老服务就这么干过，
    结果是"多打一个字符 → 字符被吞掉 → 取到了别人的包"（安全缺陷）。
    长度不是 8、或者字符集不符，就老老实实当作"没这个码"，不静默纠正。
    """
    code = raw.strip().upper()
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        return ""
    return code


def code_hint(code: str) -> str:
    """日志里用的码的"提示"：只留前 2 位，例如 `YD***`。

    为什么日志里不能打完整的码：码就是取数据的凭证，而日志谁都能看
    （同事、日志平台、运维）。线上老服务就打了完整码，等于把所有人的包挂在
    日志里 —— 谁翻日志谁就能拖走。排查时用"前 2 位 + 时间 + sha 前 8 位"够定位了。
    """
    if not code:
        return "(空)"
    return f"{code[:2]}***"


# ---------------------------------------------------------------- 存包
def inspect_package(data: bytes, filename: str) -> str:
    """只做"能不能读"的轻校验，返回包类型（`json` / `zip`）。

    为什么不全量导入：这一步用户还没登录，数据不知道该归谁；而且解包 + 写库
    要好几秒，塞在上传请求里只会让上传变成"转圈半天然后失败"。真正的导入在
    领取时做，而且只做一次。
    """
    if not data:
        raise errors.bad_request("上传的文件是空的")
    try:
        package = package_module.open_package(data, filename)
    except package_module.PackageError as exc:
        raise errors.AppError("bad_package", str(exc), http_status=400) from exc
    try:
        return package.kind
    finally:
        # ZIP 句柄一定要关，否则同时开着几十个上传会把文件描述符吃光
        package.close_quietly()


def check_capacity(db: Session, incoming: int) -> None:
    """全站总量上限检查（2 GB）。到顶回 507，说清楚怎么腾地方。

    取件码是"匿名也能往服务器塞文件"的入口，不设上限就是免费网盘。
    检查放在两处：流式上传**开始前**先粗查一次（别让用户白传 64MB 才被拒），
    真正插入前再查一次（这次带上真实大小）。
    """
    settings = get_settings()
    used = uploads_repo.total_bytes(db)
    if used + incoming > settings.upload_total_bytes:
        raise errors.AppError(
            "storage_full",
            "服务器上的取件空间满了，暂时不能上传。已经过期、没人领走的包会自动清理；"
            "如果你就是运维，跑一次 scripts/cleanup_uploads.py 可以立刻腾出空间",
            http_status=507,
        )


def create_ticket(
    db: Session,
    *,
    data: bytes,
    filename: str,
    strict_package: bool = True,
) -> UploadTicket:
    """存一个"已经在内存里"的包，返回带码的记录。**不 commit**，由调用方提交。

    `strict_package=False` 是给老接口 `/upload` 用的：那批老客户端我们改不了，
    它们传上来的东西只要不是空的就先收下 —— 校验放在它们自己解析的时候，
    在服务端拒掉只会让"云同步"莫名其妙断掉。类型按魔数猜（PK 开头就是 ZIP）。

    老接口真正走的是 `create_ticket_from_temp`（64MB 不读进内存），
    这个函数是给 multipart 路径（`/v1/uploads`，和 `/v1/imports` 一样）用的。
    """
    source = inspect_package(data, filename) if strict_package else _guess_source(data)
    return _persist(
        db,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        source=source,
        filename=filename,
        payload=data,
        temp_path=None,
    )


def create_ticket_from_temp(
    db: Session,
    *,
    temp_path: Path,
    size: int,
    sha256: str,
    filename: str = "",
) -> UploadTicket:
    """存一个"已经分块落到临时文件"的包（老接口 /upload 用）。**不 commit**。

    为什么单独一条路：老客户端传的包有 60MB，读进内存再写盘的话
    峰值约等于"包的体积 × 同时上传的人数"，单进程服务扛不住。
    调用方先流式写到 `temp_path`（顺手算好 sha256 和大小），这里只负责：
    插库拿码 → `os.replace` 到最终位置（同一块盘上是原子改名，不会出现半个包）。
    """
    source = _guess_source_from_path(temp_path)
    return _persist(
        db,
        sha256=sha256,
        size=size,
        source=source,
        filename=filename,
        payload=None,
        temp_path=temp_path,
    )


def _persist(
    db: Session,
    *,
    sha256: str,
    size: int,
    source: str,
    filename: str,
    payload: bytes | None,
    temp_path: Path | None,
) -> UploadTicket:
    """落库 + 落盘的唯一入口。两个方向都要能回滚：

    * 库插进去了、文件没写成 → 抛异常，调用方的事务不会提交，库里那条随之消失
      （文件这边再补一刀删掉，防"写了一半的文件"躺在盘上）；
    * 文件写成了、库没提交成功 → `commit_or_discard` 把文件删掉。

    顺序是"先插库再写文件"而不是反过来：码是库生成的，不知道码就不知道文件叫什么。
    """
    settings = get_settings()
    if size <= 0:
        raise errors.bad_request("上传的文件是空的")
    check_capacity(db, size)

    ticket = uploads_repo.create(
        db,
        code_factory=new_code,
        sha256=sha256,
        filename=(filename or "")[:255],
        size=size,
        source=source,
        expires_at=utcnow() + timedelta(hours=settings.upload_ttl_hours),
    )
    try:
        if payload is not None:
            uploads_repo.write_file(settings.upload_dir, ticket.code, payload)
        elif temp_path is not None:
            uploads_repo.move_file(settings.upload_dir, ticket.code, temp_path)
    except OSError as exc:
        # 盘写不进去（满、权限、临时目录和 upload_dir 不在同一块盘）：
        # 把可能已经写了一半的目标文件删掉，再把错抛出去让事务回滚
        uploads_repo.remove_file(settings.upload_dir, ticket.code)
        raise errors.AppError(
            "storage_error", "服务器保存文件失败了，请稍后再试", http_status=500
        ) from exc

    logger.info(
        "取件码 %s 收下 %s %d bytes sha=%s（全站上限 %d）",
        code_hint(ticket.code),
        ticket.source,
        ticket.bytes,
        sha256[:8],
        settings.upload_total_bytes,
    )
    return ticket


def commit_or_discard(db: Session, ticket: UploadTicket) -> None:
    """提交这次存包；提交失败就把刚写下的文件删掉。

    为什么需要它：文件是先于 commit 写下去的。commit 失败（库挂了、连接断了）
    时文件已经在盘上，而库里没有对应的行 —— 那个文件永远不会被任何清理逻辑
    认领，只会占着磁盘。这里补上最后一道。
    """
    try:
        db.commit()
    except Exception:  # noqa: BLE001 —— 任何提交失败都要先清文件，再把原异常抛出去
        uploads_repo.remove_file(get_settings().upload_dir, ticket.code)
        raise


def _guess_source(data: bytes) -> str:
    """不拆包也能认出 ZIP：ZIP 文件头固定是 `PK\\x03\\x04`。其余一律当 JSON。"""
    return "zip" if data[:4] == b"PK\x03\x04" else "json"


def _guess_source_from_path(path: Path) -> str:
    """同上，但只读文件头 4 个字节（不想为了猜类型把 64MB 读回来）。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        head = b""
    return _guess_source(head)


# ---------------------------------------------------------------- 查码 / 取包
def get_ticket(db: Session, raw_code: str) -> UploadTicket:
    """查一个能用的码；不能用就抛 `ClaimCodeError`（404 不存在 / 400 过期）。

    这里查过一遍，`mark_claimed` 里还会再判一次过期 —— 前者是为了给出准确的中文
    提示，后者是为了并发下不出错（两个请求同时到达时只有一个能改到那一行）。
    """
    code = normalize_code(raw_code)
    ticket = uploads_repo.find_by_code(db, code) if code else None
    if ticket is None:
        raise ClaimCodeError(
            "no_claim_code",
            "取件码不对：没有这个码，检查一下是不是抄错了",
            http_status=404,
        )
    if ticket.expires_at <= utcnow():
        raise ClaimCodeError(
            "claim_code_expired",
            "取件码过期了（有效期 24 小时），请重新上传一次数据包",
            http_status=400,
        )
    return ticket


def fetch_payload(db: Session, raw_code: str) -> tuple[UploadTicket, bytes]:
    """按码取回**原始字节**，同时记一次领取。给老客户端的 `/fetch` 用。

    和 `claim()` 的区别很重要：这里**不导入**。老客户端拿到包之后自己解析，
    服务端插一脚反而会把它自己的流程弄乱（它可能只想要那份 JSON 做云备份）。
    所以只做三件事：判过期、计数、把字节原样还回去 —— 原样就是原样，
    不 re-serialize、不加 BOM（老客户端会 `JSON.parse`，多一个字节都可能解析失败）。
    """
    ticket = get_ticket(db, raw_code)
    if not uploads_repo.mark_claimed(db, ticket.code, device_id=None, user_id=None):
        # 查的时候还没过期、UPDATE 的时候过期了（正好卡在边界上）
        raise ClaimCodeError(
            "claim_code_expired", "取件码刚好过期了，请重新上传数据包", http_status=400
        )
    return ticket, _read_or_lost(ticket.code)


def _read_or_lost(code: str) -> bytes:
    """读盘上那个包。文件不见了要说清楚（410），不能 500 —— 那是能修的状态。"""
    try:
        return uploads_repo.read_file(get_settings().upload_dir, code)
    except FileNotFoundError as exc:
        raise ClaimCodeError(
            "claim_code_lost",
            "这个取件码的包在服务器上找不到了（可能被手工删过），请重新上传",
            http_status=410,
        ) from exc


# ---------------------------------------------------------------- 领取并导入
def claim(
    db: Session,
    *,
    code: str,
    device: Device,
    user_id: int,
) -> tuple[ImportBatch, bool]:
    """凭码把包导进这台设备/这个账号，返回 (导入批次, 是否重复导入)。**不 commit**。

    顺序是刻意的：**先原子地占一次领取，再导入**。
    反过来（先导入再记领取）的话，导入炸了就会留下"数据进来一半、码还被记成领过"
    的状态；而先占坑的话，导入炸了整条事务回滚，码还在，用户可以再来一次。

    导入本身完全复用 `imports/service.run_import` —— 幂等、撤销、批次报告、
    图片落盘、认不出的角色照样保留，这些行为在两条入口（/v1/imports 和取件码）
    必须一模一样，复制一份迟早会不一致。
    """
    settings = get_settings()
    ticket = get_ticket(db, code)

    if not uploads_repo.mark_claimed(db, ticket.code, device_id=device.id, user_id=user_id):
        # 走到这里说明这一瞬间正好过期（或者码被清理任务删了）。
        # 抢不到就是"这个码不能用了"，没有别的解释。
        raise ClaimCodeError(
            "claim_code_expired", "取件码刚好过期了，请重新上传数据包", http_status=400
        )

    data = _read_or_lost(ticket.code)
    batch, duplicate = imports_service.run_import(
        db,
        device=device,
        data=data,
        filename=ticket.filename,
        mode="merge",
        upload_dir=settings.upload_dir,
        user_id=user_id,
    )
    uploads_repo.attach_batch(db, ticket, batch.id)
    logger.info(
        "取件码 %s 被设备 %s 领取：batch=%s 卡片=%d 会话=%d 消息=%d 重复=%s",
        code_hint(ticket.code),
        device.public_id,
        batch.public_id,
        batch.card_count,
        batch.conversation_count,
        batch.message_count,
        duplicate,
    )
    return batch, duplicate


# ---------------------------------------------------------------- 清理
def cleanup(db: Session, *, limit: int | None = None) -> int:
    """删掉过期（或领过很久）的取件记录和它们盘上的包，返回删了几条。

    先删文件再删行：反过来的话，删行成功后进程被 kill 就会留下"孤儿文件"，
    谁都不知道那个文件还有没有用。反过来（文件删了、行还在）只会让那一条
    变成"码还在但取不到内容"，用户重传一次就好 —— 两种坏法里挑轻的那个。

    顺带扫掉 `.part` 临时文件（上次进程被 kill 时正在传的那半个包）。
    """
    settings = get_settings()
    rows = uploads_repo.list_expired(
        db,
        now=utcnow(),
        ttl=timedelta(hours=settings.upload_ttl_hours),
        limit=limit,
    )
    for row in rows:
        uploads_repo.remove_file(settings.upload_dir, row.code)
    removed = uploads_repo.delete_codes(db, [row.code for row in rows])
    removed += _clean_partials(settings.upload_dir, timedelta(hours=settings.upload_ttl_hours))
    if removed:
        logger.info("清理了 %d 条过期取件记录/临时文件", removed)
    return removed


def _clean_partials(upload_dir: str, older_than: timedelta) -> int:
    """删掉很久以前留下的 `.part` 临时文件（正常流程里它们会被立刻改名或删除）。"""
    cutoff = utcnow().timestamp() - older_than.total_seconds()
    removed = 0
    try:
        for path in uploads_repo.path_dir(upload_dir).glob(f"*{PARTIAL_SUFFIX}"):
            if path.stat().st_mtime < cutoff:
                uploads_repo.remove_path(path)
                removed += 1
    except OSError as exc:
        # 目录还不存在（一次都没人上传过）也会走到这里 —— 不算错误
        logger.warning("扫临时文件失败：%s", exc)
    return removed


def lazy_cleanup(db: Session) -> None:
    """上传时顺手清一小批。**失败一律吞掉** —— 清理是后台杂活，不能耽误用户传包。"""
    try:
        cleanup(db, limit=LAZY_CLEANUP_LIMIT)
    except Exception as exc:  # noqa: BLE001 —— 惰性清理出什么错都不该影响这次上传
        logger.warning("顺手清理取件码失败（不影响这次上传）：%s", exc)


__all__ = [
    "ClaimCodeError",
    "check_capacity",
    "claim",
    "cleanup",
    "commit_or_discard",
    "create_ticket",
    "create_ticket_from_temp",
    "fetch_payload",
    "get_ticket",
    "inspect_package",
    "lazy_cleanup",
    "new_code",
    "normalize_code",
]
