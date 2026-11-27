"""tests/test_uploads.py —— 取件码：匿名传包 → 注册/登录后凭码把数据领走。

这一组测试盯的不是"接口能不能通"，而是**数据最后落在谁名下、领了几次、
出错时有没有留下半截状态**：

  * 码是 8 位、只含约定字符集（用户要手抄，抄错一个就是"没这个码"）
  * 注册带码：数据直接进新账号，同一个事务；码不对 → 整次注册回滚
  * 码**不是一次性**的：同设备重复领 → duplicate=true；换设备再领 → 各得一份
  * 同一个包传两次 → 两个独立码；领取时按 sha256 幂等，数据只进来一份
  * 过期/不存在 → 400/404；空文件/超大/坏包 → 400/413
  * 未登录不能领（401）；上传按 IP 限流（429）；全站总量到顶 507
  * OPTIONS 预检要有 CORS 头（安卓 APK 的 origin 是 https://localhost）

测试自己的隔离：每个用例一个临时 upload_dir + 清空 upload_tickets。
为什么不加在 conftest 里 —— conftest 是公共文件（这一轮的改动范围不含它），
而取件码是这组测试自己的东西，自己扫自己的地，不会影响别人的用例。
"""

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from app.db import SessionLocal
from app.settings import get_settings

PASSWORD = "baker-pass-2026"

# 码的字符集（和 app/uploads.py 的 CODE_ALPHABET 一致，也就是线上老服务那张 32 字符表：
# 去掉 0 O 1 I，保留 L —— 老客户端的格式校验认 L）。测试里写死一份，
# 是为了"改了实现忘了改约定"时能立刻报错，而不是跟着实现一起漂移。
CODE_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每个用例一个独立的取件目录，并在开始前清空取件表。

    不这么做的话：上一个用例的包还占着 2GB 额度、文件也会堆在项目的
    `_runtime/uploads` 里，跑几轮之后"为什么突然 507"就变成玄学了。
    """
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    with SessionLocal() as db:
        db.execute(text("TRUNCATE TABLE upload_tickets"))
        db.commit()
    yield


# ---------------------------------------------------------------- 造数据
def _bundle(text_body: str = "在吗") -> bytes:
    """一个最小的合法 JSON 包（和 tests/test_imports.py 里的形状一致）。"""
    payload = {
        "version": 1,
        "myGender": "male",
        "cards": [
            {
                "conversations": [
                    {
                        "name": "伊冯",
                        "messages": [
                            {"id": 1, "side": "mine", "text": text_body},
                            {"id": 2, "side": "other", "text": "在。说吧"},
                        ],
                    }
                ]
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _zip_bundle() -> bytes:
    """一个最小的 ZIP 包（project.json + 一段对话），用来验 source=zip。"""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "project.json",
            json.dumps({"version": 1, "myGender": "male"}, ensure_ascii=False),
        )
        archive.writestr(
            "cards/00/conversations/00-伊冯.json",
            json.dumps(
                {"name": "伊冯", "messages": [{"side": "other", "text": "嗯"}]},
                ensure_ascii=False,
            ),
        )
    return buffer.getvalue()


def _upload(
    client: TestClient,
    data: bytes,
    *,
    filename: str = "BAKER-20261118.json",
) -> Response:
    """**不带** Authorization 上传 —— 这就是"还没注册就能传包"的核心场景。"""
    return client.post(
        "/v1/uploads",
        files={"file": (filename, data, "application/json")},
    )


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_device(client: TestClient, platform: str = "pytest") -> dict[str, str]:
    body = client.post("/v1/auth/device", json={"platform": platform}).json()
    return _auth_header(body["token"])


def _register(
    client: TestClient,
    auth: dict[str, str],
    *,
    username: str = "clover",
    claim_code: str | None = None,
) -> Response:
    payload: dict[str, object] = {
        "username": username,
        "password": PASSWORD,
        "display_name": "三叶草",
    }
    if claim_code is not None:
        payload["claim_code"] = claim_code
    return client.post("/v1/auth/register", headers=auth, json=payload)


def _registered_device(client: TestClient, username: str = "clover") -> dict[str, str]:
    """注册一台设备 + 账号，返回它的 Authorization 头（领取接口要求登录）。"""
    auth = _new_device(client)
    resp = _register(client, auth, username=username)
    assert resp.status_code == 201, resp.text
    return auth


def _titles(client: TestClient, auth: dict[str, str]) -> list[str]:
    rows = client.get("/v1/conversations", headers=auth).json()["items"]
    return [row["title"] for row in rows]


def _user_count() -> int:
    with SessionLocal() as db:
        return int(db.scalar(text("SELECT COUNT(*) FROM users")) or 0)


def _expire(code: str) -> None:
    """把某个码改成"一小时前就过期了"，用来测过期分支（不用真等 24 小时）。"""
    with SessionLocal() as db:
        db.execute(
            text(
                "UPDATE upload_tickets SET expires_at = NOW() - INTERVAL 1 HOUR "
                "WHERE code = :code"
            ),
            {"code": code},
        )
        db.commit()


# ---------------------------------------------------------------- 上传
def test_upload_returns_code_and_metadata(client: TestClient) -> None:
    """上传成功只回四个字段：码、过期时间、字节数、包类型。

    码的字符集也要卡：用户是**照着手抄**的，如果混进 0/O/1/I/L，
    抄错的概率会高到没法用。
    """
    bundle = _bundle()
    resp = _upload(client, bundle)
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert set(body) == {"code", "expires_at", "bytes", "source"}
    assert len(body["code"]) == 8
    assert all(ch in CODE_CHARSET for ch in body["code"])
    assert body["bytes"] == len(bundle)
    assert body["source"] == "json"

    info = client.get(f"/v1/uploads/{body['code']}")
    assert info.status_code == 200, info.text
    meta = info.json()
    assert meta["code"] == body["code"]
    assert meta["claimed"] is False
    assert meta["claim_count"] == 0
    # 元信息里**不许有内容**（这个接口是给"先验证码对不对"用的）
    assert "data" not in meta
    assert "content" not in meta


def test_upload_accepts_zip_package(client: TestClient) -> None:
    """ZIP 包也要能传（判定靠内容魔数，不只看扩展名）。"""
    resp = _upload(client, _zip_bundle(), filename="BAKER.zip")
    assert resp.status_code == 201, resp.text
    assert resp.json()["source"] == "zip"


def test_upload_rejects_empty_and_broken(client: TestClient) -> None:
    """空文件 400、坏包 400 —— 而且都不该在库里留下一行。"""
    empty = _upload(client, b"")
    assert empty.status_code == 400, empty.text

    broken = _upload(client, b"this is not a package")
    assert broken.status_code == 400, broken.text
    assert broken.json()["error"]["code"] == "bad_package"

    with SessionLocal() as db:
        left = db.scalar(text("SELECT COUNT(*) FROM upload_tickets"))
    assert left == 0


def test_upload_rejects_oversized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 import_max_bytes → 413（把上限调小，别真造一个 64MB 的文件）。"""
    monkeypatch.setattr(get_settings(), "import_max_bytes", 1024)
    resp = _upload(client, b"{" + b" " * 2048 + b"}")
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "too_large"


def test_upload_rejects_when_storage_full(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全站总量到顶 → 507，而且提示里要说清楚怎么清理。"""
    monkeypatch.setattr(get_settings(), "upload_total_bytes", 10)
    resp = _upload(client, _bundle())
    assert resp.status_code == 507, resp.text
    assert resp.json()["error"]["code"] == "storage_full"
    assert "cleanup_uploads" in resp.json()["error"]["message"]


def test_upload_is_rate_limited(client: TestClient) -> None:
    """每个 IP 每小时 5 个包；第 6 个 429。

    限流按 IP 而不是按账号：上传这一步**还没有账号**，IP 是唯一能认的东西。
    """
    for _ in range(5):
        assert _upload(client, _bundle()).status_code == 201

    blocked = _upload(client, _bundle())
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["error"]["code"] == "too_many_uploads"


# ---------------------------------------------------------------- 注册带码
def test_register_with_claim_code_lands_data_in_account(client: TestClient) -> None:
    """核心流程：匿名传包 → 拿码 → 注册带码 → 数据直接在新账号名下。"""
    auth = _new_device(client)
    code = _upload(client, _bundle()).json()["code"]

    resp = _register(client, auth, claim_code=code)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # 响应里带这次领取的导入结果（形状和 /v1/imports 一样）
    assert body["import_result"]["conversations"] == 1
    assert body["import_result"]["messages"] == 2
    assert body["import_result"]["duplicate"] is False

    assert _titles(client, auth) == ["伊冯"]

    # 码被记成领过，但**没有作废**（同一小时里还能被别的设备领）
    meta = client.get(f"/v1/uploads/{code}").json()
    assert meta["claimed"] is True
    assert meta["claim_count"] == 1


def test_register_with_bad_code_rolls_back_whole_registration(client: TestClient) -> None:
    """码不对 → 400 bad_claim_code，而且**账号一个都不能留下**。

    这是这组测试里最重要的一条：不能出现"账号建好了、数据没进来"的半截状态。
    用户看到那种状态会以为数据丢了，然后重传一次，库里就多一份重复数据。
    """
    auth = _new_device(client)
    before = _user_count()

    resp = _register(client, auth, claim_code="ZZZZZZZZ")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "bad_claim_code"

    assert _user_count() == before
    # 这台设备也仍然是访客（设备绑定没生效）
    assert client.get("/v1/auth/me", headers=auth).json()["kind"] == "guest"


def test_register_with_expired_code_rolls_back(client: TestClient) -> None:
    """过期的码同样整次回滚。"""
    auth = _new_device(client)
    code = _upload(client, _bundle()).json()["code"]
    _expire(code)

    before = _user_count()
    resp = _register(client, auth, claim_code=code)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "bad_claim_code"
    assert _user_count() == before


# ---------------------------------------------------------------- 可重复领取
def test_same_code_twice_on_same_device_is_duplicate(client: TestClient) -> None:
    """同一个码、同一台设备再领一次 → 200 + duplicate=true，数据不会多一份。

    码**不是一次性**的（产品口径）：可领条件只有"没过期"。
    同一台设备重复领之所以安全，是因为导入管线按 (设备, sha256) 幂等 ——
    第二次直接回上一次的批次。
    """
    auth = _new_device(client)
    code = _upload(client, _bundle()).json()["code"]
    assert _register(client, auth, claim_code=code).status_code == 201

    again = client.post("/v1/uploads/claim", headers=auth, json={"code": code})
    assert again.status_code == 200, again.text
    assert again.json()["duplicate"] is True

    assert _titles(client, auth) == ["伊冯"]
    assert client.get(f"/v1/uploads/{code}").json()["claim_count"] == 2


def test_same_code_can_be_claimed_by_another_account(client: TestClient) -> None:
    """同一个码换一台设备/换一个账号再领 → 各得一份，互不影响。

    老场景：一个人有两台手机，或者一家人共用一张"数据包截图"。
    码过期前允许这么用；数据各自落在自己账号下。
    """
    phone = _new_device(client, "android")
    code = _upload(client, _bundle("手机上的记录")).json()["code"]
    assert _register(client, phone, username="usera", claim_code=code).status_code == 201

    desktop = _new_device(client, "windows")
    assert _register(client, desktop, username="userb").status_code == 201
    claimed = client.post("/v1/uploads/claim", headers=desktop, json={"code": code})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["duplicate"] is False

    # 两台设备各有一份同名会话，但归属各自的账号
    assert _titles(client, phone) == ["伊冯"]
    assert _titles(client, desktop) == ["伊冯"]
    assert client.get(f"/v1/uploads/{code}").json()["claim_count"] == 2


def test_same_package_twice_makes_independent_codes(client: TestClient) -> None:
    """同一个包传两次 → 两个不同的码；领取时按 sha256 幂等，数据只进来一份。

    为什么要这样：用户传完没记下码、又传了一遍，是很常见的。给他两个码没问题，
    但数据不能进两份 —— 幂等键是"包内容 + 设备"，不是"码"。
    """
    auth = _new_device(client)
    bundle = _bundle()

    first = _upload(client, bundle).json()["code"]
    second = _upload(client, bundle).json()["code"]
    assert first != second

    assert _register(client, auth, claim_code=first).status_code == 201
    again = client.post("/v1/uploads/claim", headers=auth, json={"code": second})
    assert again.status_code == 200, again.text
    assert again.json()["duplicate"] is True

    assert len(_titles(client, auth)) == 1


# ---------------------------------------------------------------- 错误分支
def test_expired_code_is_rejected(client: TestClient) -> None:
    """过期码：查元信息 400、领取 400，而且一条数据都不许进来。"""
    auth = _registered_device(client)
    code = _upload(client, _bundle()).json()["code"]
    _expire(code)

    info = client.get(f"/v1/uploads/{code}")
    assert info.status_code == 400, info.text
    assert info.json()["error"]["code"] == "claim_code_expired"

    claimed = client.post("/v1/uploads/claim", headers=auth, json={"code": code})
    assert claimed.status_code == 400, claimed.text
    assert _titles(client, auth) == []


def test_unknown_code_is_404(client: TestClient) -> None:
    """不存在的码一律 404（不泄露"这个码存在但过期了"这类信息）。"""
    auth = _registered_device(client)
    assert client.get("/v1/uploads/AAAAAAAA").status_code == 404
    assert (
        client.post("/v1/uploads/claim", headers=auth, json={"code": "AAAAAAAA"}).status_code
        == 404
    )


def test_claim_requires_login(client: TestClient) -> None:
    """领取必须登录：没 token 401；有 token 但还是访客也 401。

    免登录领 = 数据落在匿名设备上，换台手机就找不回来了。
    """
    code = _upload(client, _bundle()).json()["code"]

    anonymous = client.post("/v1/uploads/claim", json={"code": code})
    assert anonymous.status_code == 401, anonymous.text

    guest = _new_device(client)
    as_guest = client.post("/v1/uploads/claim", headers=guest, json={"code": code})
    assert as_guest.status_code == 401, as_guest.text
    assert as_guest.json()["error"]["code"] == "no_user"


def test_code_is_normalized(client: TestClient) -> None:
    """用户抄码时可能小写、可能带空格 —— 一个**对的码**不该因此被判错。"""
    auth = _registered_device(client)
    code = _upload(client, _bundle()).json()["code"]
    messy = f"  {code.lower()} "

    assert client.get(f"/v1/uploads/{messy.strip()}").status_code == 200
    claimed = client.post("/v1/uploads/claim", headers=auth, json={"code": messy})
    assert claimed.status_code == 200, claimed.text


# ---------------------------------------------------------------- CORS
@pytest.mark.parametrize("path", ["/chat", "/group/reply", "/v1/uploads", "/upload", "/fetch"])
def test_cors_preflight_allows_browser_clients(client: TestClient, path: str) -> None:
    """预检必须 200 且带 `access-control-allow-origin: *`。

    没有这条的话：安卓 APK（WebView 的 origin 是 `https://localhost`）和网页版
    一发请求就被浏览器拦下来，而服务端日志里只有一条 405 —— 排查会绕很远。
    """
    resp = client.options(
        path,
        headers={
            "Origin": "https://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "POST" in resp.headers.get("access-control-allow-methods", "")
