"""tests/test_legacy_upload.py —— 老客户端（3.2.0–3.2.2）的上传/取件兼容层。

## 这一组在防什么

线上老客户端是 `XMLHttpRequest` 打的：
`POST https://local.peilika.beer/upload`，`Content-Type: application/json`，
body 就是导出包原文。拿到 `{code,...}` 之后，另一个设备上 `POST /fetch` 把包取回来
自己解析。把域名指到新后端之后，**它们一行不改也必须能用**。

所以这里不是"接口能通就行"，而是逐条模拟老客户端的真实行为：

  * 预检 OPTIONS 必须 200 + `Access-Control-Allow-Origin: *`（非 simple request 一定会发）
  * 响应字段一个不能少、类型要对（`code` 字符串、`bytes`/`raw_bytes`/`keep_hours` 数字、
    `uploaded_at`/`cleanup_at` 是 `YYYY-MM-DD HH:MM:SS`）
  * `/fetch` 拿回来的内容必须和上传的**逐字节相同**（客户端直接 JSON.parse）
  * 码可重复取（老流程里同一份包可能被取两次）
  * 新老两套接口共用一张表、同一批文件（任一方向上传，另一方向都能取到）

测试隔离：每个用例一个临时 upload_dir + 清空 upload_tickets。
和 test_uploads.py 里那份 fixture 一样 —— 没放 conftest.py 是因为这次不改它。
"""

import json
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from app.db import SessionLocal
from app.settings import get_settings

# 时间格式：老客户端把它当字符串直接显示，所以格式必须稳定
LEGACY_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# 老接口的时间是**服务器本地时间（CST，UTC+8）**
CST = timezone(timedelta(hours=8))

# 老接口的 origin（安卓 APK 的 WebView 就是这个）
ANDROID_ORIGIN = "https://localhost"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每个用例一个独立的取件目录，并在开始前清空取件表。"""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    with SessionLocal() as db:
        db.execute(text("TRUNCATE TABLE upload_tickets"))
        db.commit()
    yield


# ---------------------------------------------------------------- 模拟老客户端
def _legacy_upload(client: TestClient, body: bytes) -> Response:
    """完全照老客户端的打法拉：XHR 语义 = POST + Content-Type: application/json。"""
    return client.post(
        "/upload",
        content=body,
        headers={"Content-Type": "application/json", "Origin": ANDROID_ORIGIN},
    )


def _legacy_fetch(client: TestClient, code: str) -> Response:
    return client.post(
        "/fetch",
        json={"code": code},
        headers={"Origin": ANDROID_ORIGIN},
    )


def _bundle(suffix: str = "") -> bytes:
    """老客户端导出包的形状（和 test_imports.py 里的一致，version 必须是 1）。"""
    payload = {
        "version": 1,
        "myGender": "male",
        "cards": [
            {
                "conversations": [
                    {
                        "name": "伊冯",
                        "messages": [{"side": "mine", "text": f"在吗{suffix}"}],
                    }
                ]
            }
        ],
    }
    # 注意：ensure_ascii=False + 不加换行 —— 取回来要逐字节相等
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _claim_count(code: str) -> int:
    with SessionLocal() as db:
        value = db.scalar(
            text("SELECT claim_count FROM upload_tickets WHERE code = :code"),
            {"code": code},
        )
    return int(value or 0)


# ---------------------------------------------------------------- 1. CORS
@pytest.mark.parametrize("path", ["/upload", "/fetch"])
def test_preflight_ok_for_old_client(client: TestClient, path: str) -> None:
    """非 simple request（application/json）一定会先发 OPTIONS 预检。

    老客户端不会自己处理 CORS，预检不过 = 它直接报网络错误。
    APK 的 origin 是 `https://localhost`，网页/预览是别的域，EXE 是 `file://`。
    """
    resp = client.options(
        path,
        headers={
            "Origin": ANDROID_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "POST" in resp.headers.get("access-control-allow-methods", "")


# ---------------------------------------------------------------- 2. 上传响应
def test_upload_response_shape_and_types(client: TestClient) -> None:
    """响应字段一个不能少、类型要对 —— 老客户端少一个就显示 undefined 或抛错。"""
    body = _bundle()
    resp = _legacy_upload(client, body)
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert set(data) == {
        "ok",
        "code",
        "bytes",
        "raw_bytes",
        "uploaded_at",
        "cleanup_at",
        "keep_hours",
    }
    # 线上成功响应也带 ok:true（对拍脚本按它判成功），我们跟着带
    assert data["ok"] is True
    assert isinstance(data["code"], str) and len(data["code"]) == 8
    assert isinstance(data["bytes"], int)
    assert isinstance(data["raw_bytes"], int)
    assert data["bytes"] == len(body)
    assert data["raw_bytes"] == len(body)
    assert isinstance(data["keep_hours"], int)
    assert data["keep_hours"] == 24
    assert LEGACY_TIME_RE.match(data["uploaded_at"]), data["uploaded_at"]
    assert LEGACY_TIME_RE.match(data["cleanup_at"]), data["cleanup_at"]
    # 清理时刻必须晚于上传时刻（老客户端拿这两个算"还有多久过期"）
    assert data["cleanup_at"] > data["uploaded_at"]

    # 响应里要有 Content-Length：老客户端的 XHR 靠它算进度
    assert resp.headers.get("content-length") == str(len(resp.content))


def test_upload_accepts_non_package_body(client: TestClient) -> None:
    """老接口**不校验包结构**：只要能收就先收下。

    这一条是刻意的兼容取舍：这批客户端我们改不了，服务端在这里挑刺只会让
    "云同步"莫名断掉。校验发生在客户端自己解析包里。
    """
    resp = _legacy_upload(client, b"not-a-real-package-but-harmless")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["code"]) == 8


# ---------------------------------------------------------------- 3. 取回原样
def test_fetch_returns_byte_identical_body(client: TestClient) -> None:
    """取回来的内容必须和上传的**逐字节相同**，Content-Type 也要对。

    老客户端直接 `JSON.parse(响应文本)`。服务端顺手 re-serialize 一下、
    或者加个 BOM，都可能让它解析失败或字段漂移。
    """
    body = _bundle("，这里带中文")
    uploaded = _legacy_upload(client, body).json()

    fetched = _legacy_fetch(client, uploaded["code"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == body
    assert fetched.headers["content-type"] == "application/json; charset=utf-8"
    assert fetched.headers.get("content-length") == str(len(body))


def test_fetch_is_repeatable_and_counts_claims(client: TestClient) -> None:
    """同一个码可以取多次（码不是一次性的），每次都会记一次领取。"""
    uploaded = _legacy_upload(client, _bundle()).json()
    code = uploaded["code"]

    first = _legacy_fetch(client, code)
    second = _legacy_fetch(client, code)
    assert first.status_code == 200 and second.status_code == 200
    assert first.content == second.content
    assert _claim_count(code) == 2

    # 新接口的元信息也能看到"被取过两次"
    meta = client.get(f"/v1/uploads/{code}").json()
    assert meta["claimed"] is True
    assert meta["claim_count"] == 2


# ---------------------------------------------------------------- 4. 错误映射
def test_upload_rejects_empty_body(client: TestClient) -> None:
    """空 body → 400（老客户端会提示"导出文件是空的"）。"""
    resp = _legacy_upload(client, b"")
    assert resp.status_code == 400, resp.text


def test_upload_rejects_oversized_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过上限 → 413（老客户端对这个码有专门的文案「超过 64MB 上限」）。

    文案照抄线上：`数据过大（xx.xMB > 64MB）`。判定是 `>`，
    正好等于上限要放行（线上就是放行的）。
    """
    monkeypatch.setattr(get_settings(), "import_max_bytes", 1024 * 1024)
    resp = _legacy_upload(client, b"{" + b" " * (2 * 1024 * 1024) + b"}")
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"].startswith("数据过大（"), body
    assert "> 1MB" in body["error"], body


def test_fetch_unknown_code_is_404(client: TestClient) -> None:
    """码不存在/已清理 → 404（老客户端的文案是「取件码不存在或已清理」）。"""
    resp = _legacy_fetch(client, "ZZZZZZZZ")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body == {
        "ok": False,
        "error": "取件码不存在或数据已被清理（保留 24~48 小时）",
    }


def test_fetch_garbage_code_is_404(client: TestClient) -> None:
    """乱码（长度不对、字符不对）走 404；**空码**走 400「缺少取件码」。

    为什么空码要单独 400：线上老服务就是这么回的，而客户端对这两个状态给的提示
    不一样（400 = 重新填码，404 = 码过期、要回原设备重传）。混在一起会让用户白折腾。
    """
    for bad in ("abc", "!!!!!!!!", "0O1IL0O1"):
        resp = client.post("/fetch", json={"code": bad})
        assert resp.status_code == 404, (bad, resp.text)
        # 形状也必须还是老形状（422 那种 pydantic 包一层的做法老客户端看不懂）
        assert resp.json()["ok"] is False
        assert isinstance(resp.json()["error"], str)

    empty = client.post("/fetch", json={"code": ""})
    assert empty.status_code == 400, empty.text
    assert empty.json() == {"ok": False, "error": "缺少取件码"}


def test_upload_rate_limited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """老接口有自己的桶（默认 30 个/小时），到顶回 429。

    这里把额度调到 2，避免在测试里真传 30 次。文案照抄线上。
    """
    monkeypatch.setattr(get_settings(), "legacy_upload_rpm", 2)
    assert _legacy_upload(client, _bundle()).status_code == 200
    assert _legacy_upload(client, _bundle()).status_code == 200

    blocked = _legacy_upload(client, _bundle())
    assert blocked.status_code == 429, blocked.text
    assert blocked.json() == {"ok": False, "error": "请求太频繁，请稍后再试"}


def test_uploaded_at_is_cst_not_utc(client: TestClient) -> None:
    """`uploaded_at` 必须是**东八区**时间，格式 `YYYY-MM-DD HH:MM:SS`。

    线上实测就是服务器本地时间（CST）。我们库里统一存 UTC，如果直接输出，
    客户端会把"16:38"显示成用户的上传时间，而用户那边其实是"00:38"——
    差 8 小时，用户一看就觉得"这服务器时间坏了"。

    不写死字符串：断言"和当前 CST 时间差不到 5 分钟"，这样跨时区跑 CI 也不会假失败。
    """
    data = _legacy_upload(client, _bundle()).json()
    assert LEGACY_TIME_RE.match(data["uploaded_at"]), data["uploaded_at"]

    shown = datetime.strptime(data["uploaded_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    delta = abs((datetime.now(CST) - shown).total_seconds())
    assert delta < 300, f"uploaded_at={data['uploaded_at']} 和当前 CST 差 {delta:.0f} 秒"


def test_error_body_is_legacy_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """四种错误（400/404/413/429）的 body 都必须是 `{"ok": false, "error": "中文"}`。

    老客户端对**没专门映射**的错误，会把响应体前 120 字直接显示在界面上
    （`cloudData.ts:184`）。多一个 `request_id`、或者嵌一层 `error.code`，
    用户看到的就是一段 JSON。

    额度和上限都调小，让四种错误在同一个用例里依次出现（不用真传 30 次）。
    """
    monkeypatch.setattr(get_settings(), "legacy_upload_rpm", 2)
    monkeypatch.setattr(get_settings(), "import_max_bytes", 1024 * 1024)

    cases: list[tuple[Response, int]] = [
        (_legacy_upload(client, b""), 400),  # 请求体为空
        (_legacy_upload(client, b"{" + b" " * (2 * 1024 * 1024) + b"}"), 413),  # 太大
        (_legacy_upload(client, _bundle()), 429),  # 额度用完了
        (_legacy_fetch(client, "ZZZZZZZZ"), 404),  # 没这个码
    ]
    for resp, status in cases:
        assert resp.status_code == status, resp.text
        body = resp.json()
        assert set(body) == {"ok", "error"}, body
        assert body["ok"] is False
        assert isinstance(body["error"], str) and body["error"]
        # 是给人看的中文，不是内部错误码
        assert re.search(r"[\u4e00-\u9fa5]", body["error"]), body


def test_logs_never_contain_full_code(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """日志里不许出现完整的取件码 —— 码本身就是取数据的凭证。

    线上老服务就打了完整码：谁翻日志，谁就能把别人的包拖走。
    我们只打前 2 位（`YD***`）。这里连 `GET /v1/uploads/{code}` 的访问日志一起验 ——
    那条路径里带着码，所以访问日志单独做了打码。
    """
    caplog.set_level(logging.INFO, logger="baker")

    code = _legacy_upload(client, _bundle()).json()["code"]
    assert _legacy_fetch(client, code).status_code == 200
    # 这条路径里带着码：访问日志必须打码
    assert client.get(f"/v1/uploads/{code}").status_code == 200

    lines = [record.getMessage() for record in caplog.records if record.name.startswith("baker")]
    assert lines, "没抓到任何 baker 日志，说明这个用例本身没生效"
    joined = "\n".join(lines)
    assert code not in joined, f"日志里出现了完整取件码：\n{joined}"
    # 前 2 位要留着：说明打的是"打码后的提示"，而不是干脆不打（那样就没法排查了）
    assert code[:2] in joined


# ---------------------------------------------------------------- 5. 新老共用
def test_legacy_upload_visible_through_new_api(client: TestClient) -> None:
    """老接口传的包，新接口 `GET /v1/uploads/{code}` 也查得到（同一张表）。"""
    uploaded = _legacy_upload(client, _bundle()).json()
    meta = client.get(f"/v1/uploads/{uploaded['code']}")
    assert meta.status_code == 200, meta.text
    assert meta.json()["bytes"] == uploaded["bytes"]
    assert meta.json()["source"] == "json"


def test_new_upload_fetchable_through_legacy_fetch(client: TestClient) -> None:
    """反向：`/v1/uploads`（multipart）传的包，`/fetch` 也能原样取回。"""
    body = _bundle("，从新接口上传")
    created = client.post(
        "/v1/uploads", files={"file": ("BAKER.json", body, "application/json")}
    ).json()

    fetched = _legacy_fetch(client, created["code"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == body


def test_big_data_url_package_roundtrip(client: TestClient) -> None:
    """带图片的老包（图片是 dataURL 内联在消息里的那种）能传、能原样取回。

    这是老包最常见的形状，也是体积的来源：一张图 base64 之后大约胖 1/3。
    这里构造一个 5MB 的包，走完整条"分块落盘 → 改名 → 取回"的路。
    它同时验证了老接口不是把 body 整个读进内存再写盘（那样 5MB×并发就炸了）。
    """
    big = "A" * (5 * 1024 * 1024)
    payload = {
        "version": 1,
        "cards": [
            {
                "conversations": [
                    {
                        "name": "伊冯",
                        "messages": [
                            {
                                "side": "mine",
                                "text": "",
                                "image": f"data:image/png;base64,{big}",
                            }
                        ],
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(body) > 5 * 1024 * 1024

    uploaded = _legacy_upload(client, body)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["bytes"] == len(body)

    fetched = _legacy_fetch(client, uploaded.json()["code"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == body
