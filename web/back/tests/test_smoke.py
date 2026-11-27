"""tests/test_smoke.py —— 冒烟测试：一条链路从注册走到写消息。

这不是"为了覆盖率"，而是这一刀唯一能证明"能跑"的东西：
没这个文件，就只能靠人手动 curl，改一次代码就得重新点一遍。
"""

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_reports_schema(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # schema = 契约版本；migrations = 最后一个迁移文件。两者含义不同，别混用。
    assert body["schema"] == "1"
    assert body["migrations"] is not None


def test_meta_lists_contract(client: TestClient) -> None:
    resp = client.get("/v1/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api"] == "v1"
    assert "conversations" in body["caps"]
    assert body["server_time"].endswith("+00:00")  # 时间必须是 UTC，别让客户端猜


def test_characters_needs_no_token(client: TestClient) -> None:
    resp = client.get("/v1/characters")
    assert resp.status_code == 200
    slugs = [item["id"] for item in resp.json()["items"]]
    assert "perlica" in slugs


def test_request_id_header(client: TestClient) -> None:
    resp = client.get("/health")
    assert len(resp.headers["X-Request-Id"]) == 32


def test_no_token_is_rejected(client: TestClient) -> None:
    resp = client.get("/v1/conversations")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "no_token"


def test_bad_token_is_rejected(client: TestClient) -> None:
    resp = client.get("/v1/conversations", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "bad_token"


def test_full_flow(client: TestClient, device: tuple[str, str]) -> None:
    """注册 → 建会话 → 写消息 → 拉消息。这是这一刀的"能跑"定义。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    )
    assert created.status_code == 201, created.text
    conversation = created.json()
    assert conversation["character_id"] == "perlica"
    assert conversation["next_seq"] == 0

    conv_id = conversation["id"]
    sent = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "c-1", "text": "今天面包烤糊了", "generate": False},
        headers=headers,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["seq"] == 1
    assert sent.json()["duplicate"] is False
    assert sent.json()["reply"] is None  # 这一版不出话

    listed = client.get(f"/v1/conversations/{conv_id}/messages", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert [item["seq"] for item in body["items"]] == [1]
    assert body["next_after_seq"] == 1


def test_other_device_cannot_read(client: TestClient, device: tuple[str, str]) -> None:
    """别人的会话必须 404 —— 连"存在"这个信息都不能泄露。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    ).json()["id"]

    other = client.post("/v1/auth/device", json={"platform": "pytest-2"}).json()
    other_headers = {"Authorization": f"Bearer {other['token']}"}

    resp = client.get(f"/v1/conversations/{conv_id}", headers=other_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_conversation"


def test_unknown_character_is_404(client: TestClient, device: tuple[str, str]) -> None:
    _device_id, token = device
    resp = client.post(
        "/v1/conversations",
        json={"character_id": "nobody"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_character"


def test_meta_declares_capabilities(client: TestClient) -> None:
    """能力表要能表达"明确不支持"—— 这是列表做不到的那件事。

    `web_search=false` 是关键：老客户端如果带 use_rag=false，服务端会记日志但不假装联网。
    """
    body = client.get("/v1/meta").json()
    assert body["capabilities"]["web_search"] is False
    assert body["capabilities"]["worldbook"] is True
    assert body["capabilities"]["suggestions"] is True
    assert body["capabilities"]["group_weighted"] is True
    # 老形状还在，客户端不用改
    assert "conversations" in body["caps"]
    assert "memory" in body["caps"]
