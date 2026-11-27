"""tests/test_auth.py —— 账号：注册 / 登录 / 登出 / 换机。

这一组测试真正要守住的是**归属**，不是"接口能不能通"：

  * 登录之后，同一账号的任何设备都看得见同一批会话
  * 登出之后，这台设备就看不见账号里的记录（共用手机的场景）
  * 同一台设备换账号，前一个账号的记录不能被后一个看见
  * 登录前在这台设备上聊的，登录后自动归到账号名下（不搬数据，改一列）

登录标识是**用户名**（不是邮箱）：这一版没有发信能力，邮箱的唯一用处用不上。
"""

import pytest
from fastapi.testclient import TestClient
from httpx import Response

PASSWORD = "baker-pass-2026"
USERNAME = "clover"


def _device_auth(client: TestClient, platform: str = "pytest") -> dict[str, str]:
    """注册一台新设备，返回它的 Authorization 头。"""
    body = client.post("/v1/auth/device", json={"platform": platform}).json()
    return {"Authorization": f"Bearer {body['token']}"}


def _register(
    client: TestClient,
    auth: dict[str, str],
    username: str = USERNAME,
    password: str = PASSWORD,
    display_name: str = "三叶草",
) -> Response:
    return client.post(
        "/v1/auth/register",
        headers=auth,
        json={"username": username, "password": password, "display_name": display_name},
    )


def _login(
    client: TestClient, auth: dict[str, str], username: str, password: str = PASSWORD
) -> Response:
    return client.post(
        "/v1/auth/login", headers=auth, json={"username": username, "password": password}
    )


def _new_conversation(client: TestClient, auth: dict[str, str], title: str = "") -> str:
    """用第一个角色建一个会话，返回它的 id。"""
    slug = client.get("/v1/characters").json()["items"][0]["id"]
    resp = client.post(
        "/v1/conversations", headers=auth, json={"character_id": slug, "title": title}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _conversation_ids(client: TestClient, auth: dict[str, str]) -> set[str]:
    rows = client.get("/v1/conversations", headers=auth).json()["items"]
    return {row["id"] for row in rows}


# ---------------------------------------------------------------- 注册
def test_register_binds_device_and_returns_user(client: TestClient) -> None:
    auth = _device_auth(client)
    resp = _register(client, auth)
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["user"]["username"] == USERNAME
    assert body["user"]["display_name"] == "三叶草"
    assert body["adopted_conversations"] == 0

    me = client.get("/v1/auth/me", headers=auth).json()
    assert me["kind"] == "user"
    assert me["user"]["id"] == body["user"]["id"]
    assert len(me["devices"]) == 1


def test_guest_me_has_no_user(client: TestClient) -> None:
    auth = _device_auth(client)
    me = client.get("/v1/auth/me", headers=auth).json()
    assert me["kind"] == "guest"
    assert me["user"] is None


@pytest.mark.parametrize("second", ["CLOVER", " clover "])
def test_register_username_is_case_insensitive(client: TestClient, second: str) -> None:
    """`Clover` 和 `clover` 是同一个账号 —— 靠库的 _ai_ci 排序规则，
    否则会注册出两个账号，谁都登不上自己那份数据。"""
    assert _register(client, _device_auth(client)).status_code == 201
    resp = _register(client, _device_auth(client), username=second)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "username_taken"


def test_register_accepts_chinese_username(client: TestClient) -> None:
    """允许中文用户名：用户是玩家，为了注册去拼一个英文名不合理。"""
    resp = _register(client, _device_auth(client), username="三叶草")
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["username"] == "三叶草"


@pytest.mark.parametrize("bad", ["a", "1abc", "_abc", "有 空格", "abc@", "名" * 21])
def test_register_rejects_bad_username(client: TestClient, bad: str) -> None:
    resp = _register(client, _device_auth(client), username=bad)
    assert resp.status_code in (400, 422), resp.text


def test_register_rejects_short_password(client: TestClient) -> None:
    resp = _register(client, _device_auth(client), password="1234567")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "weak_password"


def test_register_adopts_existing_device_conversations(client: TestClient) -> None:
    """登录前在这台设备上聊的记录，注册时自动归到账号名下（改一列，不搬数据）。"""
    auth = _device_auth(client)
    conversation_id = _new_conversation(client, auth)

    resp = _register(client, auth)
    assert resp.status_code == 201, resp.text
    assert resp.json()["adopted_conversations"] == 1

    # 记录还在，没被"合并""复制"出一条新的
    assert _conversation_ids(client, auth) == {conversation_id}


# ---------------------------------------------------------------- 登录
def test_login_ok_and_wrong_password(client: TestClient) -> None:
    assert _register(client, _device_auth(client)).status_code == 201

    auth = _device_auth(client)
    assert _login(client, auth, USERNAME).status_code == 200

    bad = _login(client, auth, USERNAME, password="wrong-password")
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "bad_credentials"


def test_login_unknown_username_uses_same_error(client: TestClient) -> None:
    """账号不存在和密码错必须是同一个错误码 —— 否则这就是个"查用户名有没有人用"的接口。"""
    assert _register(client, _device_auth(client)).status_code == 201
    resp = _login(client, _device_auth(client), "nobody")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "bad_credentials"


def test_login_throttled_after_repeated_failures(client: TestClient) -> None:
    """同一台设备连试十次以上要 429（防爆破的第一道闸）。"""
    assert _register(client, _device_auth(client)).status_code == 201
    auth = _device_auth(client)

    codes = [_login(client, auth, USERNAME, "nope-nope").status_code for _ in range(11)]
    assert 429 in codes


# ---------------------------------------------------------------- 跨设备 / 归属
def test_second_device_sees_same_conversations(client: TestClient) -> None:
    """换台设备登录同一账号 → 看得见之前的会话（这就是"换手机不丢记录"）。"""
    phone = _device_auth(client, "android")
    assert _register(client, phone).status_code == 201
    conversation_id = _new_conversation(client, phone, title="手机上聊的")

    desktop = _device_auth(client, "windows")
    assert _conversation_ids(client, desktop) == set()  # 新设备还没登录，什么都没有
    assert _login(client, desktop, USERNAME).status_code == 200
    assert _conversation_ids(client, desktop) == {conversation_id}

    # 账号名下两台设备
    me = client.get("/v1/auth/me", headers=desktop).json()
    assert len(me["devices"]) == 2


def test_logout_hides_account_data_then_relogin_restores(client: TestClient) -> None:
    """登出不删数据：这台设备看不见了，重新登录还在。"""
    auth = _device_auth(client)
    assert _register(client, auth).status_code == 201
    conversation_id = _new_conversation(client, auth)

    assert client.post("/v1/auth/logout", headers=auth).status_code == 204
    assert client.get("/v1/auth/me", headers=auth).json()["kind"] == "guest"
    assert _conversation_ids(client, auth) == set()

    assert _login(client, auth, USERNAME).status_code == 200
    assert _conversation_ids(client, auth) == {conversation_id}


def test_device_cannot_switch_to_another_account(client: TestClient) -> None:
    """同一台设备已经挂着 A 账号时，不能直接登录 B 账号（那是共用手机换号）。

    要换必须先登出 —— 否则悄悄改挂会把前一个人的记录露给后一个人。
    """
    first = _device_auth(client)
    assert _register(client, first, username="usera").status_code == 201

    other_device = _device_auth(client)
    assert _register(client, other_device, username="userb").status_code == 201

    resp = _login(client, first, "userb")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "device_taken"


def test_switching_account_after_logout_does_not_leak(client: TestClient) -> None:
    """登出后换另一个账号登录，前一个账号的记录不能露出来。"""
    auth = _device_auth(client)
    assert _register(client, auth, username="usera").status_code == 201
    first_conversation = _new_conversation(client, auth, title="A 的记录")

    assert client.post("/v1/auth/logout", headers=auth).status_code == 204

    other = _device_auth(client)
    assert _register(client, other, username="userb").status_code == 201
    # A 的会话挂着 A 的 user_id，B 看不到
    assert first_conversation not in _conversation_ids(client, other)

    # 重新登录 A 账号还能看到（数据没丢，只是归属换了人）
    assert _login(client, auth, "usera").status_code == 200
    assert _conversation_ids(client, auth) == {first_conversation}


def test_other_account_cannot_open_conversation_by_id(client: TestClient) -> None:
    """拿到别人的会话 id 也没用：越权一律 404，不是 403（不泄露"它存在"）。"""
    owner = _device_auth(client)
    assert _register(client, owner, username="owner").status_code == 201
    conversation_id = _new_conversation(client, owner)

    thief = _device_auth(client)
    assert _register(client, thief, username="thief").status_code == 201

    assert client.get(f"/v1/conversations/{conversation_id}", headers=thief).status_code == 404
    assert (
        client.get(f"/v1/conversations/{conversation_id}/messages", headers=thief).status_code
        == 404
    )
    assert client.delete(f"/v1/conversations/{conversation_id}", headers=thief).status_code == 404


def test_message_writes_follow_the_same_ownership(client: TestClient) -> None:
    """写消息也要认归属：登录时能写，登出后同一个会话就 404 了。"""
    auth = _device_auth(client)
    assert _register(client, auth).status_code == 201
    conversation_id = _new_conversation(client, auth)

    wrote = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers=auth,
        json={"client_msg_id": "c-1", "text": "登录状态下写的", "generate": False},
    )
    assert wrote.status_code == 200, wrote.text
    assert wrote.json()["duplicate"] is False

    assert client.post("/v1/auth/logout", headers=auth).status_code == 204
    denied = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        headers=auth,
        json={"client_msg_id": "c-2", "text": "登出后写的", "generate": False},
    )
    assert denied.status_code == 404


# ---------------------------------------------------------------- 改密码
def test_change_password(client: TestClient) -> None:
    auth = _device_auth(client)
    assert _register(client, auth).status_code == 201

    wrong = client.post(
        "/v1/auth/password",
        headers=auth,
        json={"old_password": "not-it-at-all", "new_password": "new-pass-2026"},
    )
    assert wrong.status_code == 401

    ok = client.post(
        "/v1/auth/password",
        headers=auth,
        json={"old_password": PASSWORD, "new_password": "new-pass-2026"},
    )
    assert ok.status_code == 204

    fresh = _device_auth(client)
    assert _login(client, fresh, USERNAME, "new-pass-2026").status_code == 200
    assert _login(client, fresh, USERNAME, PASSWORD).status_code == 401


def test_change_password_requires_login(client: TestClient) -> None:
    auth = _device_auth(client)
    resp = client.post(
        "/v1/auth/password",
        headers=auth,
        json={"old_password": PASSWORD, "new_password": "new-pass-2026"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "no_user"


def test_change_password_unbinds_other_devices(client: TestClient) -> None:
    """改密 = 把其它设备踢下线（本架构里的"会话失效"等价物）。"""
    phone = _device_auth(client, "android")
    assert _register(client, phone).status_code == 201
    desktop = _device_auth(client, "windows")
    assert _login(client, desktop, USERNAME).status_code == 200
    conversation_id = _new_conversation(client, phone)
    assert _conversation_ids(client, desktop) == {conversation_id}

    ok = client.post(
        "/v1/auth/password",
        headers=phone,
        json={"old_password": PASSWORD, "new_password": "new-pass-2026"},
    )
    assert ok.status_code == 204

    # 自己这台还在；别的那台被摘掉，看不见账号里的记录了
    assert client.get("/v1/auth/me", headers=phone).json()["kind"] == "user"
    assert client.get("/v1/auth/me", headers=desktop).json()["kind"] == "guest"
    assert _conversation_ids(client, desktop) == set()
    # 数据没删：那台设备用新密码登录一次就回来了
    assert _login(client, desktop, USERNAME, "new-pass-2026").status_code == 200
    assert _conversation_ids(client, desktop) == {conversation_id}


def test_hashing_gate_rejects_when_all_slots_taken() -> None:
    """哈希闸门：位置占满时立刻 429，而不是排队把请求拖到超时。

    这一条直接测闸门本身（不起 HTTP）：它挡的是"同时烧几个核"，
    属于进程内资源保护，跟"一个人试几次"的限流是两件事。
    """
    from app import errors, gates

    gate = gates.scrypt_gate()
    for _ in range(gate.slots):
        assert gate.try_enter() is True
    try:
        with pytest.raises(errors.AppError) as info, gates.guarded(gate):
            pass
        assert info.value.code == "busy"
        assert info.value.http_status == 429
    finally:
        for _ in range(gate.slots):
            gate.leave()

    # 放干净之后还能进
    with gates.guarded(gate):
        pass


# ---------------------------------------------------------------- 导入 × 登录
def test_import_while_logged_in_is_visible_on_other_device(client: TestClient) -> None:
    """登录状态下导入的老数据包归账号名下 —— 换台设备也看得见。"""
    phone = _device_auth(client, "android")
    assert _register(client, phone).status_code == 201

    bundle = (
        '{"version":1,"cards":[{"conversations":[{"name":"伊冯",'
        '"messages":[{"side":"mine","text":"在吗"}]}]}]}'
    ).encode()
    imported = client.post(
        "/v1/imports",
        headers=phone,
        files={"file": ("BAKER.json", bundle, "application/json")},
        data={"mode": "merge"},
    )
    assert imported.status_code == 201, imported.text

    desktop = _device_auth(client, "windows")
    assert _login(client, desktop, USERNAME).status_code == 200
    titles = [
        row["title"] for row in client.get("/v1/conversations", headers=desktop).json()["items"]
    ]
    assert titles == ["伊冯"]

def test_device_truncates_overlong_metadata_instead_of_500(client: TestClient) -> None:
    """设备元信息超长时按列宽截断，不能 500。

    真实由来：安卓/浏览器的 UA 与版本串经常超过 32 字，而 devices.os_ver 是
    VARCHAR(32)。以前请求模型不限长 → 数据库 DataError → 500，
    新前端"注册设备"直接失败，迁移和聊天全跟着挂。
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Device

    long_value = "x" * 200
    resp = client.post(
        "/v1/auth/device",
        json={
            "platform": long_value,
            "app_ver": long_value,
            "model": long_value,
            "os_ver": long_value,
            "tz": long_value,
            "locale": long_value,
        },
    )
    assert resp.status_code == 201, resp.text
    public_id = resp.json()["device_id"]

    with SessionLocal() as db:
        row = db.scalar(select(Device).where(Device.public_id == public_id))
        assert row is not None
        # 列宽：platform/app_ver/os_ver 32、model/tz 64、locale 16
        assert len(row.platform) == 32
        assert len(row.app_ver) == 32
        assert len(row.model) == 64
        assert len(row.os_ver) == 32
        assert len(row.tz) == 64
        assert len(row.locale) == 16
