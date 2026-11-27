"""repos/uploads.py —— 取件码表的读写 + 包文件落盘。

和 `repos/attachments.py` 的分工一样：**这里只碰"表"和"盘"**，不做业务判断
（码怎么生成、过期了该回什么错、领取时怎么导入，都在 `app/uploads.py`）。

文件怎么放：`<upload_dir>/tickets/<code>.bin`
  为什么按 code 而不是按 sha 命名：同一个包可能被传两次、拿到两个码，
  两个码要能各自独立地过期和删除。按 sha 存就得处理"删这个码会不会删掉别人
  还在用的文件"这种引用计数问题 —— 为了几兆磁盘不值得。

领取的原子性靠 `mark_claimed` 里那一条 UPDATE：
  库自己判断"过没过期"，并且 `claim_count = claim_count + 1` 是**在库里加**，
  不是在 Python 里读出来加一写回去 —— 后者在并发下会丢计数。
"""

import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import UploadTicket

logger = logging.getLogger("baker.uploads")

# 生成码时最多试几次。32^8（约 1.1e12）的分母下，撞车概率低到可以忽略；
# 留 20 次是为了"真撞了也不至于直接 500"。
CODE_ATTEMPTS = 20

# 包文件放在 upload_dir 下的哪个子目录。和附件（按 sha 分桶）分开，
# 因为这两类文件的生命周期完全不一样：附件要长期留，取件码 24 小时就该消失。
TICKET_DIR = "tickets"


def create(
    db: Session,
    *,
    code_factory: Callable[[], str],
    sha256: str,
    filename: str,
    size: int,
    source: str,
    expires_at: datetime,
) -> UploadTicket:
    """插一条取件记录，码由 `code_factory` 现生成。

    撞码就重来：唯一索引会让 INSERT 报 IntegrityError，这里换一个码再试。
    用**保存点**（`begin_nested`）而不是整段回滚，是因为调用方的事务里可能已经
    有东西了（比如注册流程里刚建好的账号）—— 整个 rollback 会把那些一起撤掉。
    """
    for _ in range(CODE_ATTEMPTS):
        code = code_factory()
        ticket = UploadTicket(
            code=code,
            sha256=sha256,
            filename=filename,
            bytes=size,
            source=source,
            created_at=utcnow(),
            expires_at=expires_at,
            last_claimed_at=None,
            claim_count=0,
            claimed_device_id=None,
            claimed_user_id=None,
            import_batch_id=None,
        )
        try:
            with db.begin_nested():
                db.add(ticket)
                db.flush()
        except IntegrityError:
            # 这里**不打码**：日志谁都能看，码就是取数据的凭证（见 app/uploads.code_hint）
            logger.info("取件码撞车了，换一个再来")
            continue
        return ticket
    # 真走到这里说明码空间快满了（或者唯一索引被写坏了），不是用户的问题
    raise RuntimeError("连续撞码 20 次，取件码空间可能出问题了")


def find_by_code(db: Session, code: str) -> UploadTicket | None:
    """按码查。码本身是唯一的，这里查不到就是"没这个码"。"""
    return db.scalar(select(UploadTicket).where(UploadTicket.code == code))


def mark_claimed(
    db: Session,
    code: str,
    *,
    device_id: int | None,
    user_id: int | None,
) -> bool:
    """抢占式地记一次领取，返回"抢到了没有"。

    一条 UPDATE 干完四件事：判过期、记时间、计数 +1、记住是谁领的。
    为什么不用"先 SELECT 看 expires_at、再 UPDATE"：两个请求同时进来时会都通过
    SELECT 那一关，然后一起写 —— 计数会丢、也给了过期码一点可乘之机。
    这里的 WHERE 里带着 `expires_at > NOW()`，MySQL 在更新时持行锁，
    同一行只有一个请求能改到，另一个回来 rowcount=0。

    `device_id/user_id` 可以是 None：老客户端的 `/fetch` 没有设备/账号概念，
    它只是"按码取一次包"，同样算一次领取（前端要显示"这个码被取过"）。
    """
    result = cast(
        CursorResult[Any],
        db.execute(
            update(UploadTicket)
            .where(UploadTicket.code == code, UploadTicket.expires_at > func.now())
            .values(
                last_claimed_at=func.now(),
                claim_count=UploadTicket.claim_count + 1,
                claimed_device_id=device_id,
                claimed_user_id=user_id,
            )
        ),
    )
    return result.rowcount == 1


def attach_batch(db: Session, ticket: UploadTicket, batch_id: int) -> None:
    """记下"最后一次领取导进了哪个批次"，排查用。不 commit。"""
    ticket.import_batch_id = batch_id
    db.flush()


def total_bytes(db: Session) -> int:
    """全站取件包一共占了多少字节（含过期但还没被清掉的）。

    为什么不按目录 `du` 算：库里的数字是权威（文件可能被手工删过），
    而且一次 SUM 比遍历几千个文件快得多。
    """
    value = db.scalar(select(func.coalesce(func.sum(UploadTicket.bytes), 0)))
    return int(value or 0)


def list_expired(
    db: Session,
    *,
    now: datetime,
    ttl: timedelta,
    limit: int | None = None,
) -> list[UploadTicket]:
    """该删的记录：已经过期，或者最后一次领取早于 `now - ttl`。

    第二个条件在默认 TTL（24h）下其实是第一个的子集，留着是为了以后 TTL 调长
    时"领过的包不必等到过期才清"—— 语义上写全，清理策略就不用跟着改。
    没领过（last_claimed_at 是 NULL）的行不会被第二个条件误伤：
    SQL 里 NULL 比较的结果是 NULL，也就是"不匹配"。
    """
    stmt = (
        select(UploadTicket)
        .where(
            or_(
                UploadTicket.expires_at <= now,
                UploadTicket.last_claimed_at <= now - ttl,
            )
        )
        .order_by(UploadTicket.id)
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def delete_codes(db: Session, codes: list[str]) -> int:
    """按码删行，返回删了几条。**文件由调用方删**（这里只认识表）。"""
    if not codes:
        return 0
    result = cast(
        CursorResult[Any], db.execute(delete(UploadTicket).where(UploadTicket.code.in_(codes)))
    )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------- 文件
def path_for(upload_dir: str, code: str) -> Path:
    """包文件在盘上的位置。`code` 一律传库里的值（不是用户输入），防目录穿越。"""
    return Path(upload_dir) / TICKET_DIR / f"{code}.bin"


def path_dir(upload_dir: str) -> Path:
    """放取件包的目录。清理任务扫 `.part` 临时文件时用。"""
    return Path(upload_dir) / TICKET_DIR


def write_file(upload_dir: str, code: str, data: bytes) -> None:
    """把包写到盘上。目录不存在就建（第一次上传时 `tickets/` 还不存在）。"""
    target = path_for(upload_dir, code)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def move_file(upload_dir: str, code: str, source: Path) -> None:
    """把已经写好的临时文件改名成正式文件。

    用 `Path.replace`（底层是同一块盘上的 rename）而不是"读出来再写一遍"：
    60MB 的包复制一遍要几十毫秒到几百毫秒、还多占一份磁盘；改名是原子的，
    中途断电也不会留下半个包。**前提：临时文件和 upload_dir 在同一块盘上**。
    """
    target = path_for(upload_dir, code)
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)


def read_file(upload_dir: str, code: str) -> bytes:
    """按码读回原始字节。文件不在就抛 FileNotFoundError（不静默返回空）。"""
    return path_for(upload_dir, code).read_bytes()


def remove_file(upload_dir: str, code: str) -> None:
    """删文件。已经不在就算了 —— 清理任务不该因为"文件早没了"而整批失败。"""
    remove_path(path_for(upload_dir, code))


def remove_path(path: Path) -> None:
    """删任意一个文件，不存在就忽略。"""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


__all__ = [
    "attach_batch",
    "create",
    "delete_codes",
    "find_by_code",
    "list_expired",
    "mark_claimed",
    "move_file",
    "path_dir",
    "path_for",
    "read_file",
    "remove_file",
    "remove_path",
    "total_bytes",
    "write_file",
]
