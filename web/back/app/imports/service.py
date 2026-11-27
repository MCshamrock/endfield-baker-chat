"""imports/service.py —— 把"拆包 / 归一化 / 写库 / 记账"串成一次完整导入。

这一层存在的理由：路由函数里不该出现"先算指纹、再查幂等、再解析、再写库"这种流程。
路由只该说"这个请求要导一个包"，流程放这里，而且要能一眼看完。

两件事刻意放在同一条事务里：
  * 覆盖模式（replace）的"先软删旧会话"
  * 新会话的写入
导到一半出错时整体回滚，包括那次软删 —— 否则用户会得到"旧的没了、新的没进来"。
"""

import hashlib

from sqlalchemy.orm import Session

from app import errors
from app.imports import apply as apply_module
from app.imports import normalize as normalize_module
from app.imports import package as package_module
from app.models import Device, ImportBatch
from app.repos import conversations as conversations_repo
from app.repos import imports as imports_repo

VALID_MODES = ("merge", "replace")


def run_import(
    db: Session,
    *,
    device: Device,
    data: bytes,
    filename: str,
    mode: str,
    upload_dir: str,
    user_id: int | None = None,
) -> tuple[ImportBatch, bool]:
    """导入一个包，返回 (批次, 是否重复导入)。**不 commit**，由调用方提交。

    重复导入（同一台设备、包内容逐字节相同）直接回上次那一条 —— 用户双击上传、
    网络重试、或者自己忘了导过，都不会把会话建两遍。
    """
    if mode not in VALID_MODES:
        raise errors.bad_request(f"mode 只能是 {'/'.join(VALID_MODES)}")
    if not data:
        raise errors.bad_request("上传的文件是空的")

    fingerprint = hashlib.sha256(data).hexdigest()
    existing = imports_repo.find_by_fingerprint(db, device.id, fingerprint)
    if existing is not None:
        return existing, True

    package = _open(data, filename)
    try:
        project = _normalize(package)
    finally:
        # 图片已经在这一步读成字节了，ZIP 句柄可以关掉
        package.close_quietly()

    batch = imports_repo.create(
        db,
        device_id=device.id,
        fingerprint=fingerprint,
        source=package.kind,
        mode=mode,
        package_version=project.version,
    )
    if mode == "replace":
        conversations_repo.soft_delete_all(db, user_id=user_id, device_id=device.id)

    result = apply_module.apply(
        db,
        device=device,
        project=project,
        batch_id=batch.id,
        upload_dir=upload_dir,
        user_id=user_id,
    )
    imports_repo.finish(db, batch, counts=result.counts(), report=result.report())
    return batch, False


def revoke_import(db: Session, *, device: Device, public_id: str) -> ImportBatch:
    """撤销一次导入：这个批次建出来的会话全部软删。

    只碰**这次导入建的**会话，用户自己聊的那些一条都不动 —— 这是有
    `conversations.import_batch_id` 的意义。重复撤销是幂等的（第二次什么都不做）。
    """
    batch = imports_repo.find_owned(db, public_id, device.id)
    if batch is None:
        raise errors.AppError("no_import", "没有这次导入记录", http_status=404)
    if batch.revoked_at is not None:
        return batch

    for row in conversations_repo.list_by_batch(db, device.id, batch.id):
        conversations_repo.soft_delete(db, row)
    imports_repo.revoke(db, batch)
    return batch


def _open(data: bytes, filename: str) -> package_module.RawPackage:
    """拆包失败统一翻成 400（包不对是用户的输入问题，不是服务器故障）。"""
    try:
        return package_module.open_package(data, filename)
    except package_module.PackageError as exc:
        raise errors.AppError("bad_package", str(exc), http_status=400) from exc


def _normalize(package: package_module.RawPackage) -> normalize_module.RawProject:
    """解析失败统一翻成 400 + 中文原因（normalize 只懂"包不对"，不懂 HTTP）。"""
    try:
        return normalize_module.normalize(package)
    except package_module.PackageError as exc:
        raise errors.AppError("bad_package", str(exc), http_status=400) from exc


__all__ = ["revoke_import", "run_import"]
