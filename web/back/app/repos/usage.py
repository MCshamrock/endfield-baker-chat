"""repos/usage.py —— 用量记账。

一个请求写一行。现在只记"这一轮花了多少 token、首字多久、总共多久"，
到第 4 刀（key 亲和）会补上 `api_key_id` 和真正的 `cached_tokens`。

记账必须在**同一事务**里和消息一起提交：
如果回复存了、账没记，成本就永久丢了；反过来也一样难看。
"""

from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import UsageEvent


def record(
    db: Session,
    *,
    request_id: str,
    device_id: int | None,
    conversation_id: int | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    first_token_ms: int = 0,
    total_ms: int = 0,
    status: str = "ok",
    api_key_id: int | None = None,
) -> UsageEvent:
    """写一行用量。调用方负责提交。"""
    event = UsageEvent(
        request_id=request_id,
        device_id=device_id,
        user_id=None,
        conversation_id=conversation_id,
        api_key_id=api_key_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        first_token_ms=first_token_ms,
        total_ms=total_ms,
        status=status,
        created_at=utcnow(),
    )
    db.add(event)
    db.flush()
    return event


__all__ = ["record"]
