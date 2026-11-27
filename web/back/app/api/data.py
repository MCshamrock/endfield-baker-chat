"""api/data.py —— 「清空服务器数据」（设置里那四个危险按钮的服务端那一半）。

为什么需要这条端点：
  客户端的「删除全部对话 / 清空全部消息 / 清空全部上下文 / 一键清空」以前**只清本机**
  （Pinia + IndexedDB）。用户实测发现"这几个好像不管服务器的数据"—— 换个设备
  一登录，会话和消息全回来了。所以清理必须两头一起做。

契约（前端 `utils/dataClear.ts` 按这个写，改形状要两边一起改）：

    POST /v1/data/clear          （要设备登录：Authorization: Bearer <token>）
    {"conversations": false, "messages": false, "context": false}
        conversations=true → 删本归属者的**全部会话**（连带消息/摘要/群成员）
        messages=true      → 删本归属者的**全部消息**，会话留着，`next_seq` 归零
        context=true       → 删摘要 + 干员记忆，并清掉会话上的摘要/记忆副本

    200 {"deleted": {"conversations": n, "messages": n, "summaries": n, "memories": n}}
    400 三个 flag 全是 false（前端传了个"什么都不做"的请求，多半是 bug）
    401 没有/无效的设备 token

两条硬规矩：
  1. **作用域只有"我自己"** —— 归属判定走 `repos/data_clear.owner_scope`，
     与聊天链路同一套（登录按账号、访客按设备）。别人的数据在 SQL 层就不存在。
  2. **整个过程一个事务** —— 用户看到的是"要么全清、要么全没清"，
     不存在"会话删了消息还在"的中间态。
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app import errors
from app.deps import CurrentIdentity, DbSession
from app.repos import data_clear as data_clear_repo

logger = logging.getLogger("baker.api")

router = APIRouter(tags=["data"])


class DataClearRequest(BaseModel):
    """要清哪几类。默认全 false —— 请求体不写字段等于"什么都不清"，会被 400 挡掉。"""

    conversations: bool = False
    messages: bool = False
    context: bool = False


class DataClearCounts(BaseModel):
    """各表实际删掉的行数（前端直接显示成"会话 N、消息 N、摘要 N、记忆 N"）。"""

    conversations: int = 0
    messages: int = 0
    summaries: int = 0
    memories: int = 0


class DataClearOut(BaseModel):
    deleted: DataClearCounts


@router.post("/data/clear", response_model=DataClearOut)
def clear_data(
    req: DataClearRequest, db: DbSession, identity: CurrentIdentity
) -> DataClearOut:
    """一次性清掉本归属者的数据。**不可恢复**，前端必须二次确认。"""
    if not (req.conversations or req.messages or req.context):
        # 参数全 false：与其"假装成功"返回一排 0，不如直接说这请求没意义
        raise errors.bad_request(
            "至少要清一类数据：conversations / messages / context 不能全是 false"
        )

    try:
        counts = data_clear_repo.clear(
            db,
            user_id=identity.user_id,
            device_id=identity.device.id,
            owner_key=identity.owner_key,
            conversations=req.conversations,
            messages=req.messages,
            context=req.context,
        )
        db.commit()
    except Exception:
        # 一个事务：任何一步失败，整批都不算数（不留"会话没了消息还在"的中间态）
        db.rollback()
        logger.exception(
            "数据清理失败 device=%s user=%s",
            identity.device.public_id,
            identity.user_id,
        )
        raise

    # 审计日志：谁、清了什么、删了多少行。这条是真出事后唯一能查的东西
    logger.info(
        "数据清理 device=%s user=%s conversations=%s messages=%s context=%s "
        "deleted=%s",
        identity.device.public_id,
        identity.user_id,
        req.conversations,
        req.messages,
        req.context,
        counts,
    )

    return DataClearOut(deleted=DataClearCounts(**counts))
