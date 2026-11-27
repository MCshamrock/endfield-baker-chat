"""api/ —— 路由层：一个文件对应一组端点。

每个路由函数的形状都长这样，多余的事一件不做：

    1. 解析请求（pydantic 已经校验过）
    2. 调 repos/ 里的函数
    3. 决定 commit 还是 rollback
    4. 组装响应

**这一层不许写 SQL**，也不许 `datetime.now()`。
"""

from app.api.admin import router as admin_router
from app.api.attachments import router as attachments_router
from app.api.auth import router as auth_router
from app.api.characters import router as characters_router
from app.api.conversations import router as conversations_router
from app.api.data import router as data_router
from app.api.imports import router as imports_router
from app.api.legacy import router as legacy_router
from app.api.legacy_group import router as legacy_group_router
from app.api.memories import router as memories_router
from app.api.meta import router as meta_router
from app.api.rag_compat import router as rag_compat_router
from app.api.uploads import alias_router as uploads_alias_router
from app.api.uploads import router as uploads_router

__all__ = [
    "admin_router",
    "attachments_router",
    "auth_router",
    "characters_router",
    "conversations_router",
    "data_router",
    "imports_router",
    "legacy_group_router",
    "legacy_router",
    "memories_router",
    "meta_router",
    "rag_compat_router",
    "uploads_alias_router",
    "uploads_router",
]
