"""gates.py —— 会烧 CPU 的操作要排队（和"限流"是两件事）。

限流管的是"一个人试多少次"，这里管的是"同一时刻有几个请求在算哈希"。

为什么需要：scrypt 一次要吃 16 MB 内存、几十毫秒 CPU。登录接口是**不需要登录**的，
任何人都能打。没有闸门的话，几百个并发请求会同时申请几百个 16 MB 的缓冲 ——
进程不是被"刷爆带宽"，而是被自己的内存申请拖死。

参考值来自另一个站的实测：把同时在算的哈希压到 2 个之后，正常用户感觉不到变化
（一次登录 40ms，排队也只是多等一个 40ms），而攻击方拿不到任何并行收益。

实现用 `threading` 而不是 asyncio：路由函数是同步的（FastAPI 把它们丢进线程池跑），
在异步上下文里 `acquire` 一个线程信号量反而会阻塞事件循环。
"""

import contextlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from app import errors

# 同一时刻最多几个哈希在算。2 是"够用且不炸内存"的值
SCRYPT_SLOTS = 2


class Gate:
    """容量固定的闸门。`try_enter` 拿不到位置就立刻返回 False（不排队、不阻塞）。"""

    __slots__ = ("_semaphore", "slots")

    def __init__(self, slots: int) -> None:
        self.slots = max(1, slots)
        self._semaphore = threading.BoundedSemaphore(self.slots)

    def try_enter(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def leave(self) -> None:
        # 多 release 一次说明调用方写错了；不值得让用户 500，记在心里就行
        with contextlib.suppress(ValueError):
            self._semaphore.release()


_scrypt_gate: Gate | None = None


def scrypt_gate() -> Gate:
    """全局唯一的哈希闸门（进程内）。"""
    global _scrypt_gate
    if _scrypt_gate is None:
        _scrypt_gate = Gate(SCRYPT_SLOTS)
    return _scrypt_gate


@contextmanager
def guarded(gate: Gate) -> Iterator[None]:
    """占一个位置干活；占不到就回 429（而不是排长队把请求超时拖死）。"""
    if not gate.try_enter():
        raise errors.AppError(
            "busy",
            "服务器正忙着校验密码，等一两秒再试",
            http_status=429,
            details={"retry_after": 1},
        )
    try:
        yield
    finally:
        gate.leave()


def reset() -> None:
    """测试用：换一个新的闸门（上一个可能还被某个用例占着）。"""
    global _scrypt_gate
    _scrypt_gate = None


__all__ = ["Gate", "guarded", "reset", "scrypt_gate"]
