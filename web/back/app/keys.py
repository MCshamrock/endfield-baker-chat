"""keys.py —— 上游 key 池：选谁、粘谁、罚谁。

三件事，对应三个真实后果：

1. **会话粘性**（最重要的那条）。同一段对话尽量一直用同一个 key：
   上游对同一 key 的同一段前缀有 KV 缓存，换 key = 缓存全废 = 首字慢好几倍。
   所以先试 `conversations.api_key_id`，只有它不可用才换，换了立刻回写绑定。

2. **令牌桶**。每个 key 每分钟 `rpm` 次；用桶而不是计数器，
   是因为桶能在请求间隙自然回填，不会被"这一分钟刚好卡在边界"惩罚。

3. **惩罚分级**。错分三种，处理完全不同：
   * 429（限速）→ 冷却 60 秒，别换 key 重试，因为限速是按 key 全局生效的；
   * 401/402/403（认证/欠费/封禁）→ 直接禁用，重试只是浪费；
   * 5xx / 网络 → 冷却 10 秒，可能是上游抖动。

池子的状态（桶、冷却）在内存，重启就重置 —— 这没问题：冷却是"暂时不打它"，
重启后重新探一次比一直躲着更合理。只有"禁用"和"冷却截止时间"必须写库，
因为它们代表人工判断，重启不该忘。
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app import crypto
from app.clock import utcnow
from app.models import ApiKey
from app.repos import api_keys as api_keys_repo
from app.settings import get_settings


@dataclass(slots=True)
class Credential:
    """一次上游调用要用的凭证（明文只在内存里活一次请求的时间）。"""

    key: str
    base_url: str
    model: str
    key_id: int | None = None
    label: str = ""

    def mask(self) -> str:
        """日志里只出现这个（前后各 4 位），永远不打全码。"""
        if len(self.key) < 10:
            return "****"
        return f"{self.key[:4]}****{self.key[-4:]}"


@dataclass
class KeyState:
    """一个 key 的运行时状态。"""

    credential: Credential
    rpm: int
    tokens: float
    last_refill: float
    cooldown_until: float
    last_used: float
    disabled: bool = False
    inflight: int = field(default=0)

    def refill(self, now: float) -> None:
        """按经过的时间回填令牌（上限 = rpm）。"""
        elapsed = max(0.0, now - self.last_refill)
        if elapsed <= 0:
            return
        capacity = float(self.rpm)
        self.tokens = min(capacity, self.tokens + elapsed * (capacity / 60.0))
        self.last_refill = now

    def usable(self, now: float) -> bool:
        return (
            not self.disabled
            and self.cooldown_until <= now
            and self.inflight < 1  # 单 key 单并发：老后端也是这么限的
        )


class KeyPool:
    """进程内 key 池。没有多线程锁：单进程 + 事件循环，选号是纯同步的几行代码。"""

    def __init__(self, states: list[KeyState]) -> None:
        self.states = states

    # ---------------------------------------------------------------- 选号
    def acquire(self, preferred_id: int | None) -> KeyState | None:
        """挑一个能用的 key：优先会话绑定的那个，否则挑"站点优先 + 剩得最多"的。"""
        now = time.monotonic()
        for state in self.states:
            state.refill(now)

        if preferred_id is not None:
            for state in self.states:
                if state.credential.key_id == preferred_id and state.usable(now) and state.tokens >= 1:
                    return self._take(state, now)

        candidates = [s for s in self.states if s.usable(now) and s.tokens >= 1]
        if not candidates:
            return None
        # 排序三档：
        #   1. 站点优先 —— 国内站实测比国际站快一倍（1.52s vs 3.10s）
        #   2. 剩得多 —— 别把某一把打死
        #   3. 最久没用 —— 同站点内形成**轮询**，不会每次都压在同一把上
        candidates.sort(
            key=lambda s: (_site_rank(s.credential.base_url), -s.tokens, s.last_used)
        )
        return self._take(candidates[0], now)

    @staticmethod
    def _take(state: KeyState, now: float) -> KeyState:
        state.tokens -= 1
        state.last_used = now
        state.inflight += 1
        return state

    @staticmethod
    def release(state: KeyState) -> None:
        if state.inflight > 0:
            state.inflight -= 1

    # ---------------------------------------------------------------- 奖惩
    def on_success(self, db: Session, state: KeyState) -> None:
        credential = state.credential
        if credential.key_id is not None:
            row = _row(db, credential.key_id)
            if row is not None:
                api_keys_repo.mark_success(db, row)

    def on_failure(
        self, db: Session, state: KeyState, *, status: int | None, network: bool = False
    ) -> None:
        """按错误类型惩罚。status=None 且 network=True 表示连接层失败。"""
        if status == 429:
            self._cooldown(db, state, get_settings().key_cooldown_seconds)
            return
        if status in (401, 402, 403):
            self._disable(db, state)
            return
        if network or (status is not None and status >= 500):
            self._soft_fail(db, state)

    def _cooldown(self, db: Session, state: KeyState, seconds: float) -> None:
        """429：冷却一段时间。限速按 key 全局生效，换 key 重试只会更快撞墙。"""
        state.cooldown_until = time.monotonic() + seconds
        self._persist_cooldown(db, state.credential, seconds)

    def _disable(self, db: Session, state: KeyState) -> None:
        """401/402/403：凭证/欠费/封禁，重试没有意义，直接停用到人工处理。"""
        state.disabled = True
        if state.credential.key_id is None:
            return
        row = _row(db, state.credential.key_id)
        if row is not None:
            row.enabled = 0
            db.flush()

    def _soft_fail(self, db: Session, state: KeyState) -> None:
        """5xx / 网络：短冷却 + 记失败次数，连续太多次才禁用。"""
        state.cooldown_until = time.monotonic() + 10.0
        if state.credential.key_id is None:
            return
        row = _row(db, state.credential.key_id)
        if row is None:
            return
        fails = api_keys_repo.mark_failure(db, row)
        if fails >= get_settings().key_fail_limit:
            row.enabled = 0
            db.flush()

    @staticmethod
    def _persist_cooldown(db: Session, credential: Credential, seconds: float) -> None:
        if credential.key_id is None:
            return
        row = _row(db, credential.key_id)
        if row is not None:
            until = utcnow().replace(microsecond=0) + timedelta(seconds=seconds)
            api_keys_repo.mark_cooldown(db, row, until)


def _row(db: Session, key_id: int) -> ApiKey | None:
    return db.get(ApiKey, key_id)


def _site_rank(base_url: str) -> int:
    """站点优先级：命中优先名单的排前面（0），其余排后面（1）。

    实测（2026-11-15，同一句话各打三次）：
      api.agnes-ai.cn   平均 1.52s
      apihub.agnes…     平均 3.10s
    国内站快一倍，所以默认优先打它；它没号了再落到国际站，而不是反过来。
    """
    prefers = [item.strip() for item in get_settings().upstream_prefer_hosts.split(",") if item.strip()]
    for index, host in enumerate(prefers):
        if host in base_url:
            return index
    return len(prefers) + 1


# ---------------------------------------------------------------- 全局池
_pool: KeyPool | None = None
_built_at: float = 0.0
_BUILD_TTL_SECONDS = 10.0


def get_pool(db: Session) -> KeyPool:
    """拿池子（10 秒内复用）。库里没配 key 时退回到环境变量里的单 key。"""
    global _pool, _built_at
    now = time.monotonic()
    if _pool is not None and now - _built_at < _BUILD_TTL_SECONDS:
        return _pool

    states: list[KeyState] = []
    settings = get_settings()
    for row in api_keys_repo.list_usable(db):
        try:
            plain = crypto.decrypt(row.key_enc, settings.secret)
        except Exception:  # noqa: BLE001 —— 单条解不开不能让整个池子塌掉
            continue
        states.append(
            KeyState(
                credential=Credential(
                    key=plain,
                    base_url=row.base_url or settings.upstream_base_url,
                    model=row.model or settings.upstream_model,
                    key_id=row.id,
                    label=row.label,
                ),
                rpm=max(1, row.rpm or settings.key_rpm_default),
                tokens=float(max(1, row.rpm or settings.key_rpm_default)),
                last_refill=now,
                cooldown_until=0.0,
                last_used=0.0,
            )
        )

    if not states and settings.upstream_api_key:
        states.append(
            KeyState(
                credential=Credential(
                    key=settings.upstream_api_key,
                    base_url=settings.upstream_base_url,
                    model=settings.upstream_model,
                    key_id=None,
                    label="env",
                ),
                rpm=settings.key_rpm_default,
                tokens=float(settings.key_rpm_default),
                last_refill=now,
                cooldown_until=0.0,
                last_used=0.0,
            )
        )

    _pool = KeyPool(states)
    _built_at = now
    return _pool


async def acquire_waiting(
    pool: KeyPool, preferred_id: int | None, wait_seconds: float
) -> KeyState | None:
    """借一个 key；暂时没有就**等一会儿**，而不是立刻失败。

    为什么要有等待：每个 key 是单并发（同时只允许一个请求在跑），所以"可用 key 数"
    天然就是并发上限。如果借不到就直接报错，等于用"挂了几个 key"来限制线上人数 ——
    那是我们不想做的事。

    等待是异步的（`asyncio.sleep`），等的人不占线程、不占连接的处理权，
    只占一个还没返回的响应 —— 二十个人一起等也不会有二十个线程。

    超过 `wait_seconds` 还是借不到才算真的忙（此时多半是上游整体限流或 key 全禁用），
    返回 None 让调用方给一句能看懂的错。
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        state = pool.acquire(preferred_id)
        if state is not None:
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.2, remaining))


def reset() -> None:
    """丢掉池子，下次重建（测试用；后台改完 key 也可以调一下让它立刻生效）。"""
    global _pool, _built_at
    _pool = None
    _built_at = 0.0


__all__ = ["Credential", "KeyPool", "KeyState", "get_pool", "reset"]
