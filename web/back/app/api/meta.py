"""api/meta.py —— `GET /v1/meta`：告诉客户端"我是谁、支持什么"。

这个端点是整个契约的地基：客户端先问它，再决定显示哪些按钮。
所以这里只报**真的能用**的能力（settings.caps），不报计划中的。
"""

from fastapi import APIRouter

from app.clock import to_iso_str, utcnow
from app.schemas import MetaOut
from app.settings import get_settings

router = APIRouter(tags=["meta"])


@router.get("/meta", response_model=MetaOut)
def get_meta() -> MetaOut:
    """返回版本、最低客户端版本、能力列表、服务端时间。"""
    settings = get_settings()
    return MetaOut(
        api=settings.api_version,
        schema=settings.schema_version,
        app_min=settings.app_min,
        product=settings.product,
        caps=settings.caps,
        capabilities=settings.capabilities,
        server_time=to_iso_str(utcnow()),
    )
