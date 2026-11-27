"""api/imports.py —— 数据包导入。老客户端 / 老网页「导出数据」出来的包，从这里进来。

三个端点：

    POST   /v1/imports            multipart：`file` + `mode=merge|replace`
                                  → 导入结果（含"未收录角色"这类明细）
    GET    /v1/imports            这台设备的导入历史（最近 50 次）
    DELETE /v1/imports/{id}       撤销这次导入：它建的会话全部软删

为什么导入要绑定设备：新后端的身份就是设备。包本身是匿名的（谁都能导自己那份），
所以"导进谁名下"由 Bearer token 决定，包里的内容不参与身份判断。

为什么 POST 用 multipart 而不是 JSON body：包可能是 60MB 的 ZIP，
base64 塞进 JSON 会再胖 33%，而且服务端还得先解一层 base64 才能判断是不是 ZIP。
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Form, UploadFile

from app import errors
from app.clock import to_iso, to_iso_str
from app.deps import CurrentIdentity, DbSession
from app.imports import service
from app.models import ImportBatch
from app.repos import imports as imports_repo
from app.schemas import ImportListOut, ImportResultOut
from app.settings import get_settings

logger = logging.getLogger("baker.imports")

router = APIRouter(tags=["imports"])

# 每次从上传流里读多少字节。1MB 是"内存峰值"和"系统调用次数"的折中
READ_CHUNK = 1024 * 1024


@router.post("/imports", response_model=ImportResultOut, status_code=201)
async def create_import(
    file: UploadFile,
    db: DbSession,
    identity: CurrentIdentity,
    mode: Annotated[str, Form()] = "merge",
) -> ImportResultOut:
    """导入一个数据包。同一个包重复上传会回上次的结果（`duplicate=true`）。

    登录状态下导入的会话直接归到账号名下，换台设备也看得见。
    """
    settings = get_settings()
    data = await _read_whole_file(file, settings.import_max_bytes)

    batch, duplicate = service.run_import(
        db,
        device=identity.device,
        data=data,
        filename=file.filename or "",
        mode=mode,
        upload_dir=settings.upload_dir,
        user_id=identity.user_id,
    )
    db.commit()

    logger.info(
        "数据包导入 %s source=%s mode=%s cards=%d conversations=%d messages=%d images=%d dup=%s",
        batch.public_id,
        batch.source,
        batch.mode,
        batch.card_count,
        batch.conversation_count,
        batch.message_count,
        batch.image_count,
        duplicate,
    )
    return _to_out(batch, duplicate=duplicate)


@router.get("/imports", response_model=ImportListOut)
def list_imports(db: DbSession, identity: CurrentIdentity) -> ImportListOut:
    """这台设备的导入记录。用户想"我刚导的是哪一次、能不能撤"时看这个。

    导入批次是按**设备**记的（不是按账号）：换台设备导入是另一件事，
    混在一起反而看不出"这个包是从哪台机器搬过来的"。
    """
    rows = imports_repo.list_for_device(db, identity.device.id)
    return ImportListOut(items=[_to_out(row) for row in rows])


@router.delete("/imports/{import_id}", response_model=ImportResultOut)
def revoke_import(
    import_id: str, db: DbSession, identity: CurrentIdentity
) -> ImportResultOut:
    """撤销这次导入。只删它建的会话，用户自己聊的一条都不动。"""
    batch = service.revoke_import(db, device=identity.device, public_id=import_id)
    db.commit()
    return _to_out(batch)


async def _read_whole_file(file: UploadFile, limit: int) -> bytes:
    """分块读上传内容，边读边算总量，超上限立刻停。

    为什么不用 `await file.read()` 一次读完：一个 200MB 的"包"会直接把进程内存
    吃满（单进程服务，没有别的 worker 帮忙扛）。分块读能在超限的第一兆就拒绝。
    """
    buffer = bytearray()
    while True:
        chunk = await file.read(READ_CHUNK)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise errors.AppError(
                "too_large",
                f"数据包太大了（上限 {limit // (1024 * 1024)} MB）。"
                "老客户端导出时会自动控制体积，超过这个数说明文件不对",
                http_status=413,
            )
    if not buffer:
        raise errors.bad_request("上传的文件是空的")
    return bytes(buffer)


def _to_out(batch: ImportBatch, duplicate: bool = False) -> ImportResultOut:
    """批次 → 对外形状。明细（notes）从批次里存的 report 取，没有就是空列表。"""
    report = batch.report or {}
    notes = report.get("notes") if isinstance(report, dict) else None
    return ImportResultOut(
        id=batch.public_id,
        duplicate=duplicate,
        source=batch.source,
        mode=batch.mode,
        package_version=batch.package_version,
        cards=batch.card_count,
        conversations=batch.conversation_count,
        messages=batch.message_count,
        images=batch.image_count,
        skipped=batch.skipped_count,
        notes=[str(item) for item in notes] if isinstance(notes, list) else [],
        created_at=to_iso_str(batch.created_at),
        revoked_at=to_iso(batch.revoked_at),
    )
