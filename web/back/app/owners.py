"""owners.py —— "这份数据归谁"的唯一写法。

一个人可能是登录账号（`u:<user_id>`），也可能只是台没登录的设备（`d:<device_id>`）。
会话、附件、干员记忆、发言权重全都按这个键归属，所以只在这里写一次。

为什么不用 user_id / device_id 双列：
  双列就要在每个查询里写 `WHERE (user_id = :u OR (user_id IS NULL AND device_id = :d))`，
  少写一个括号就是越权。一个字符串键，查起来就是一列等值匹配，也不会写错。
"""


def owner_key(user_id: int | None, device_id: int) -> str:
    """登录优先：登录了就按账号算，没登录才按设备算。"""
    return f"u:{user_id}" if user_id else f"d:{device_id}"


__all__ = ["owner_key"]
