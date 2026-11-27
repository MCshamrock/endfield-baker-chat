"""tests/conftest.py —— 测试的公共准备。

三件事，顺序不能乱：

1. **先算出测试库地址、写进环境变量，再 import app**。
   `app.db` 在 import 时就把连接串定下来了（engine 是模块级对象）。
   晚一行设置，测试就会跑到开发库上 —— 那是最容易发生、后果最严重的一类失误。

2. 跑一次迁移，保证测试库结构是最新的。

3. 每个测试前清表，保证互不影响。角色表**不清**（测试依赖那 3 个种子角色）。

测试库怎么来的：拿 `BAKER_DATABASE_URL`（或 .env 里那行）的库名后面加 `_test`。
这样本地（3307 的 baker_back_test）和服务器（3306 的 baker_back_test）都不用改代码 ——
早期版本把地址写死过，一到服务器就得改测试文件，那是设计错误。
"""

import os

from sqlalchemy.engine import make_url

# 开发默认值，只在既没环境变量也没 .env 时兜底
_FALLBACK_URL = (
    "mysql+pymysql://baker:baker_local_2026@127.0.0.1:3307/baker_back?charset=utf8mb4"
)


def _base_database_url() -> str:
    """取"主库地址"：环境变量 > .env 文件 > 内置默认值。"""
    if os.environ.get("BAKER_DATABASE_URL"):
        return os.environ["BAKER_DATABASE_URL"]
    try:
        from dotenv import dotenv_values  # pydantic-settings 自带这个依赖

        value = dotenv_values(".env").get("BAKER_DATABASE_URL")
        if value:
            return value
    except Exception:  # noqa: BLE001 —— 读不到 .env 不是错误，用默认值即可
        pass
    return _FALLBACK_URL


def _test_database_url() -> str:
    """把主库名加 `_test` 后缀，得到测试库地址。"""
    url = make_url(_base_database_url())
    if not url.database:
        raise RuntimeError("BAKER_DATABASE_URL 里没有库名，无法推导测试库")
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


TEST_DATABASE_URL = _test_database_url()

# ---- 必须在 import app 之前设置（见上面第 1 条）
os.environ["BAKER_DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import gates, ratelimit  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.migrate import apply_all  # noqa: E402
from app.repos import content as content_repo  # noqa: E402

# 每个测试前要清空的表（子表在前，父表在后；顺序对了就不用关外键检查）
_TABLES_TO_CLEAN = [
    "messages",
    "summaries",
    "conversation_members",
    "import_batches",
    "conversations",
    "character_memories",
    "character_weights",
    "memory_settings",
    "device_prompts",
    "devices",
    "users",
    "attachments",
    "usage_events",
    "jobs",
    "config",
    "api_keys",
]


@pytest.fixture(scope="session", autouse=True)
def migrate_test_db() -> None:
    """整个测试会话只跑一次迁移。"""
    try:
        apply_all(TEST_DATABASE_URL)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "测试库没准备好。先跑：pwsh -File scripts/dev_mysql.ps1 createdb\n"
            f"原始错误：{exc}"
        ) from exc


@pytest.fixture(autouse=True)
def clean_tables() -> None:
    """每个测试开始前把业务表清空，避免上个测试的数据影响这个。

    顺带清掉内容层的缓存（安全协议有 60 秒缓存）—— 不清的话，
    上一个测试写进去的协议会在下一个测试里"幽灵般"存在。
    """
    content_repo.clear_cache()
    ratelimit.reset()  # 限流计数是进程内的，跨用例会累积
    gates.reset()  # 哈希闸门也要换新的：上一个用例可能还占着位置
    with SessionLocal() as db:
        db.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in _TABLES_TO_CLEAN:
            db.execute(text(f"TRUNCATE TABLE {table}"))
        db.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        db.commit()


@pytest.fixture
def client() -> TestClient:
    """ASGI 直连的测试客户端：不起真实端口，跑得快。"""
    return TestClient(app)


@pytest.fixture
def device(client: TestClient) -> tuple[str, str]:
    """注册一台测试设备，返回 (device_id, token)。"""
    resp = client.post("/v1/auth/device", json={"platform": "pytest"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["device_id"], body["token"]
