"""api/attachments.py —— 图片上传与取回。

三个端点：

    POST /v1/attachments   multipart，字段名 `file`
        → {id, mime, bytes, sha256, url}
    GET  /v1/attachments/{id}
        → 原始字节（带 Content-Type）
    GET  /v1/attachments
        → 这台设备（登录后是整个账号）传过的图，最近 50 张

三条规矩：

1. **只收图片，且限大小**。非图片、超 6 MB 直接拒 —— 不然后端就成了免费图床。
2. **文件按内容去重，记录按上传者各留一条**。同一张图传两次只存一份文件，
   第二次回原来那条记录（`dedup: true`）。
3. **取回要鉴权，且只给归属者**：访客只能取自己设备的图；登录后可以取
   **同账号任何设备**传的图（否则手机发的图换到电脑上就是裂图）。
   别人的图一律 404（不是 403，不泄露"这张图存在"）。
"""

import logging

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import Response

from app import errors
from app.deps import CurrentIdentity, DbSession
from app.repos import attachments as attachments_repo
from app.schemas import AttachmentOut
from app.settings import get_settings

logger = logging.getLogger("baker.attachments")

router = APIRouter(tags=["attachments"])

ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@router.post("/attachments", response_model=AttachmentOut, status_code=201)
async def upload_attachment(
    file: UploadFile, db: DbSession, identity: CurrentIdentity
) -> AttachmentOut:
    """上传一张图。同内容重复上传会回原来的 id。"""
    settings = get_settings()

    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in ALLOWED_MIME:
        raise errors.AppError(
            "unsupported_type",
            f"只收图片（png/jpeg/webp/gif），收到的是 {mime or '未知类型'}",
            http_status=415,
        )

    data = await file.read()
    if not data:
        raise errors.bad_request("文件是空的")
    if len(data) > settings.max_upload_bytes:
        raise errors.AppError(
            "too_large",
            f"图片太大了（上限 {settings.max_upload_bytes // (1024 * 1024)} MB）",
            http_status=413,
        )

    attachment, created = attachments_repo.save(
        db,
        upload_dir=settings.upload_dir,
        data=data,
        mime=mime,
        device_id=identity.device.id,
    )
    db.commit()
    logger.info(
        "附件上传 %s %s %s bytes dedup=%s",
        attachment.public_id,
        mime,
        attachment.bytes,
        not created,
    )

    return AttachmentOut(
        id=attachment.public_id,
        mime=attachment.mime,
        bytes=attachment.bytes,
        sha256=attachment.sha256,
        url=f"/v1/attachments/{attachment.public_id}",
        dedup=not created,
    )


@router.get("/attachments/{attachment_id}")
def get_attachment(
    attachment_id: str, db: DbSession, identity: CurrentIdentity
) -> Response:
    """取回归属者的图。别人的图一律 404（不泄露存在性）。"""
    settings = get_settings()
    attachment = attachments_repo.find_owned(db, attachment_id, identity.device_ids)
    if attachment is None:
        raise errors.AppError("no_attachment", "图片不存在", http_status=404)

    try:
        payload = attachments_repo.read_bytes(settings.upload_dir, attachment)
    except FileNotFoundError as exc:
        # 库里有行、盘上没文件：说明被手工删过。回 410 而不是 500，便于排查。
        raise errors.AppError(
            "attachment_lost", "这张图在服务器上找不到了", http_status=410
        ) from exc

    return Response(
        content=payload,
        media_type=attachment.mime,
        headers={
            # 内容按 sha 命名，永不变化，可以长缓存
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": attachment.sha256,
        },
    )


@router.get("/attachments")
def list_attachments(
    db: DbSession, identity: CurrentIdentity, request: Request
) -> dict[str, object]:
    """归属者传过的图（按时间倒序，最多 50 条）。"""
    rows = attachments_repo.list_for_devices(db, identity.device_ids)
    return {
        "items": [
            {
                "id": row.public_id,
                "mime": row.mime,
                "bytes": row.bytes,
                "url": f"/v1/attachments/{row.public_id}",
            }
            for row in rows
        ]
    }
