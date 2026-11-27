"""tests/test_keys.py —— key 池：粘性、令牌桶、惩罚分级。

这三条任何一条错，表现都是"回复变慢/偶发 429"，而且很难从日志看出来 ——
所以必须自动化验。
"""

import pytest
from sqlalchemy import text

from app import crypto, keys
from app.db import SessionLocal
from app.keys import Credential
from app.repos import api_keys as api_keys_repo
from app.settings import get_settings


def _seed_keys(count: int, rpm: int = 10) -> list[int]:
    """往库里塞几把测试 key（真加密，跟生产同一条路径）。"""
    secret = get_settings().secret
    ids: list[int] = []
    with SessionLocal() as db:
        for index in range(count):
            row = api_keys_repo.upsert(
                db,
                label=f"test-{index}",
                key_enc=crypto.encrypt(f"sk-test-{index}", secret),
                base_url="https://example.invalid/v1",
                model="m",
                rpm=rpm,
            )
            ids.append(row.id)
        db.commit()
    keys.reset()
    return ids


def test_env_fallback_when_table_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """库里没有 key 时，用环境变量里那把（本地调试的常态）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "upstream_api_key", "sk-env-only")
    keys.reset()

    with SessionLocal() as db:
        pool = keys.get_pool(db)
        state = pool.acquire(None)

    assert state is not None
    assert state.credential.key_id is None  # 环境变量这把没有数据库 id
    assert state.credential.key == "sk-env-only"


def test_prefers_bound_key() -> None:
    """会话绑定了哪把，就优先给哪把 —— 这是前缀缓存命中的前提。"""
    ids = _seed_keys(3)
    with SessionLocal() as db:
        pool = keys.get_pool(db)
        state = pool.acquire(preferred_id=ids[2])
    assert state is not None
    assert state.credential.key_id == ids[2]


def test_spreads_when_no_preference() -> None:
    """没有绑定偏好时，不该永远给第一把（那会把单个 key 打满）。"""
    _seed_keys(3)
    picked: set[int] = set()
    with SessionLocal() as db:
        pool = keys.get_pool(db)
        for _ in range(3):
            state = pool.acquire(None)
            assert state is not None
            picked.add(int(state.credential.key_id or 0))
            pool.release(state)
    # 三把容量相同、都"最久没用" → 会依次轮到三把不同的 key
    assert len(picked) == 3


def test_rate_limit_puts_key_on_cooldown() -> None:
    """429 → 冷却，这一轮之后就不该再选中它。"""
    ids = _seed_keys(1)
    with SessionLocal() as db:
        pool = keys.get_pool(db)
        state = pool.acquire(None)
        assert state is not None
        pool.on_failure(db, state, status=429)
        pool.release(state)
        assert pool.acquire(None) is None  # 唯一一把在冷却

        row = db.execute(text("SELECT cooldown_until FROM api_keys WHERE id = :i"), {"i": ids[0]}).scalar()
    assert row is not None  # 冷却时间落库了（重启也不忘）


def test_auth_error_disables_key_permanently() -> None:
    """401/402/403 → 直接禁用，等人工处理（重试只会重复报错）。"""
    ids = _seed_keys(1)
    with SessionLocal() as db:
        pool = keys.get_pool(db)
        state = pool.acquire(None)
        assert state is not None
        pool.on_failure(db, state, status=401)
        pool.release(state)
        db.commit()

        enabled = db.execute(text("SELECT enabled FROM api_keys WHERE id = :i"), {"i": ids[0]}).scalar()
    assert enabled == 0


def test_acquire_waiting_times_out() -> None:
    """所有 key 都占满时，等到超时就放弃 —— 而不是立刻报错，也不是无限等。"""
    import asyncio

    from app.keys import KeyPool, KeyState, acquire_waiting

    state = KeyState(
        credential=Credential(key="sk-x", base_url="http://x", model="m"),
        rpm=10,
        tokens=10.0,
        last_refill=0.0,
        cooldown_until=0.0,
        last_used=0.0,
        inflight=1,  # 占满
    )
    pool = KeyPool([state])
    assert asyncio.run(acquire_waiting(pool, None, wait_seconds=0.1)) is None


def test_acquire_waiting_gets_key_when_freed() -> None:
    """等的时候有人释放了，就立刻拿到（不拒人，只让请求排一会儿）。"""
    import asyncio

    from app.keys import KeyPool, KeyState, acquire_waiting

    state = KeyState(
        credential=Credential(key="sk-x", base_url="http://x", model="m"),
        rpm=10,
        tokens=10.0,
        last_refill=0.0,
        cooldown_until=0.0,
        last_used=0.0,
        inflight=1,
    )
    pool = KeyPool([state])

    async def scenario() -> bool:
        task = asyncio.create_task(acquire_waiting(pool, None, wait_seconds=1.0))
        await asyncio.sleep(0.05)
        pool.release(state)  # 释放
        got = await task
        return got is not None

    assert asyncio.run(scenario()) is True


def test_acquire_waiting_returns_immediately_when_free() -> None:
    import asyncio

    from app.keys import KeyPool, KeyState, acquire_waiting

    state = KeyState(
        credential=Credential(key="sk-x", base_url="http://x", model="m"),
        rpm=10,
        tokens=10.0,
        last_refill=0.0,
        cooldown_until=0.0,
        last_used=0.0,
    )
    pool = KeyPool([state])
    assert asyncio.run(acquire_waiting(pool, None, wait_seconds=1.0)) is not None


def test_prefers_domestic_site_and_round_robins() -> None:
    """站点优先 + 同站轮询。

    实测国内站快一倍（1.52s vs 3.10s），所以：
      * 有国内站可用时，一个国际站都不该被选中；
      * 国内站有多把时，要轮流用，不能每次压在同一把上。
    """
    from app.keys import KeyPool, KeyState

    def make(key_id: int, base_url: str) -> KeyState:
        return KeyState(
            credential=Credential(
                key=f"sk-{key_id}", base_url=base_url, model="m", key_id=key_id
            ),
            rpm=10,
            tokens=10.0,
            last_refill=0.0,
            cooldown_until=0.0,
            last_used=0.0,
        )

    pool = KeyPool(
        [
            make(1, "https://apihub.agnes-ai.com/v1/"),
            make(2, "https://api.agnes-ai.cn/v1/"),
            make(3, "https://api.agnes-ai.cn/v1/"),
            make(4, "https://apihub.agnes-ai.com/v1/"),
        ]
    )

    picked = []
    for _ in range(2):
        state = pool.acquire(None)
        assert state is not None
        picked.append(state.credential.key_id)
        pool.release(state)

    assert picked == [2, 3], f"应当先国内站且轮询，实际 {picked}"

    # 两把国内站都用过一轮后，第三把才该落到国际站
    third = pool.acquire(None)
    assert third is not None
    # 国内站令牌还算充足，所以仍然是国内站（轮询第 1 把）
    assert third.credential.base_url == "https://api.agnes-ai.cn/v1/"
    pool.release(third)


def test_falls_back_to_international_when_domestic_exhausted() -> None:
    """国内站都不可用（冷却/没令牌）时，才落到国际站。"""
    from app.keys import KeyPool, KeyState

    def make(key_id: int, base_url: str, cooldown: float = 0.0) -> KeyState:
        return KeyState(
            credential=Credential(
                key=f"sk-{key_id}", base_url=base_url, model="m", key_id=key_id
            ),
            rpm=10,
            tokens=10.0,
            last_refill=0.0,
            cooldown_until=cooldown,
            last_used=0.0,
        )

    import time as _time

    pool = KeyPool(
        [
            # 冷却截止时间用的是 monotonic 时钟，所以要基于"现在"往后加
            make(2, "https://api.agnes-ai.cn/v1/", cooldown=_time.monotonic() + 10_000.0),
            make(1, "https://apihub.agnes-ai.com/v1/"),
        ]
    )
    state = pool.acquire(None)
    assert state is not None
    assert state.credential.key_id == 1
    pool.release(state)


def test_success_resets_failure_count() -> None:
    ids = _seed_keys(1)
    with SessionLocal() as db:
        pool = keys.get_pool(db)
        state = pool.acquire(None)
        assert state is not None
        pool.on_failure(db, state, status=500)
        pool.on_success(db, state)
        pool.release(state)
        db.commit()

        fails = db.execute(text("SELECT fail_count FROM api_keys WHERE id = :i"), {"i": ids[0]}).scalar()
    assert fails == 0
