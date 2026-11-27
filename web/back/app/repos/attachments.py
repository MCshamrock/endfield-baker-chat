"""repos/attachments.py —— 附件表的读写 + 文件落盘。

存两份东西：
  * 文件本体：`<upload_dir>/<sha 前两位>/<sha>.<ext>`（按内容分桶，避免单目录几万文件）
  * 数据库行：public_id（对外）、sha256（去重键）、mime、字节数、尺寸、相对路径

去重靠 `sha256` 唯一索引：同一张图传两次只存一份，第二次直接回原来那条。
"""

import base64
import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.ids import new_public_id
from app.models import Attachment


def find_by_sha(db: Session, sha256: str) -> Attachment | None:
    """按内容找一条（任意设备）。只用于统计/排查，不再拿它做去重决策。"""
    return db.scalar(select(Attachment).where(Attachment.sha256 == sha256))


def find_by_device_sha(db: Session, device_id: int, sha256: str) -> Attachment | None:
    """同一台设备、同一内容 —— 这才是"重复上传"的判据。

    跨设备不去重记录：文件只有一份（路径按 sha 命名），但归属必须各记各的，
    否则 A 传的图 B 用不了（取回会按归属拒掉）。
    """
    return db.scalar(
        select(Attachment).where(
            Attachment.device_id == device_id, Attachment.sha256 == sha256
        )
    )


def find_by_public_id(db: Session, public_id: str) -> Attachment | None:
    return db.scalar(select(Attachment).where(Attachment.public_id == public_id))


def find_owned(db: Session, public_id: str, device_ids: list[int]) -> Attachment | None:
    """按 public_id 找"归属者能看的"图。

    归属按设备集合判而不是单台：登录之后，同一账号的另一台设备也要能看这张图，
    否则在手机上发的图，换到电脑上就变成裂图。
    """
    if not device_ids:
        return None
    return db.scalar(
        select(Attachment).where(
            Attachment.public_id == public_id,
            Attachment.device_id.in_(device_ids),
        )
    )


def list_for_devices(db: Session, device_ids: list[int], limit: int = 50) -> list[Attachment]:
    """归属者名下所有设备传过的图，最近的在前。"""
    if not device_ids:
        return []
    stmt = (
        select(Attachment)
        .where(Attachment.device_id.in_(device_ids))
        .order_by(Attachment.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def public_ids_by_id(db: Session, attachment_ids: list[int]) -> dict[int, str]:
    """主键 → 对外 id。拉消息历史时一次换掉，免得每条消息再查一次附件表。

    客户端拿到 attachment_id（这里是 public_id）就能去 `GET /v1/attachments/{id}`
    取图 —— 消息里只存自增主键，对外永远只给 public_id。
    """
    if not attachment_ids:
        return {}
    stmt = select(Attachment.id, Attachment.public_id).where(
        Attachment.id.in_(attachment_ids)
    )
    return {row[0]: row[1] for row in db.execute(stmt).all()}


def save(
    db: Session,
    *,
    upload_dir: str,
    data: bytes,
    mime: str,
    device_id: int,
) -> tuple[Attachment, bool]:
    """落盘 + 入库。返回 (附件, 是否新建)。

    先算 sha256 再决定写不写文件：同内容第二次上传时，文件已经在了，直接复用。
    """
    sha = hashlib.sha256(data).hexdigest()

    # 同一设备重复上传同一张图：直接回原来那条（文件也早就在了）
    same_device = find_by_device_sha(db, device_id, sha)
    if same_device is not None:
        return same_device, False

    suffix = _suffix_for(mime)
    relative = f"{sha[:2]}/{sha}{suffix}"
    target = Path(upload_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # 换设备上传同一张图：文件复用，不重写；只是多一条归属记录
    file_existed = target.exists()
    if not file_existed:
        target.write_bytes(data)

    attachment = Attachment(
        public_id=new_public_id(),
        sha256=sha,
        device_id=device_id,
        mime=mime,
        bytes=len(data),
        width=0,
        height=0,
        path=relative,
        created_at=utcnow(),
    )
    db.add(attachment)
    db.flush()
    # 返回值第二项 = "是否新建了记录"（不是"文件是否新写"）：
    # 换设备上传同一张图会复用文件、但记录是新的，对这台设备来说它就是一张新图。
    return attachment, True


def read_bytes(upload_dir: str, attachment: Attachment) -> bytes:
    """按库里的相对路径读回文件。文件丢了会抛 FileNotFoundError（不静默返回空）。"""
    return (Path(upload_dir) / attachment.path).read_bytes()


def to_data_url(upload_dir: str, attachment: Attachment) -> str:
    """转成 data URL，给上游多模态用（OpenAI 兼容的 image_url）。"""
    payload = base64.b64encode(read_bytes(upload_dir, attachment)).decode("ascii")
    return f"data:{attachment.mime};base64,{payload}"


def _suffix_for(mime: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mapping.get(mime, ".bin")


__all__ = [
    "find_by_public_id",
    "find_by_sha",
    "find_owned",
    "list_for_devices",
    "public_ids_by_id",
    "read_bytes",
    "save",
    "to_data_url",
]
