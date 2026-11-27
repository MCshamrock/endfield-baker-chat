"""tests/test_idempotency.py —— 幂等：同一个 client_msg_id 连发多次，库里只有一条。

为什么单独一个文件：
  弱网重发是这个项目最常见的真实场景（手机信号差、用户连点两次、客户端超时重试）。
  出问题时的表现是"对话里凭空多出一条重复的话"，用户会以为撞鬼了。
  这条性质必须能自动验，而不是靠人手动点。
"""

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal


def _count_messages(conversation_id: str) -> int:
    """直接数库里的行数 —— 不信接口返回，只看数据库真有什么。"""
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT COUNT(*) FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.public_id = :public_id"
            ),
            {"public_id": conversation_id},
        ).scalar()
    return int(row or 0)


def test_same_client_msg_id_only_one_row(
    client: TestClient, device: tuple[str, str]
) -> None:
    """连发 10 次同一个 id：库里 1 条，第 2 次起都回 duplicate=true 且 seq 相同。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    ).json()["id"]

    payload = {"client_msg_id": "c-dup", "text": "在吗", "generate": False}
    first = client.post(f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False

    for _ in range(9):
        again = client.post(
            f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers
        )
        assert again.status_code == 200
        assert again.json()["duplicate"] is True
        assert again.json()["seq"] == first.json()["seq"]

    assert _count_messages(conv_id) == 1


def test_different_ids_append_in_order(
    client: TestClient, device: tuple[str, str]
) -> None:
    """不同 id 的消息按顺序追加，seq 从 1 连续递增。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    ).json()["id"]

    seqs = []
    for index in range(5):
        resp = client.post(
            f"/v1/conversations/{conv_id}/messages",
            json={"client_msg_id": f"c-{index}", "text": f"第 {index} 句", "generate": False},
            headers=headers,
        )
        seqs.append(resp.json()["seq"])

    assert seqs == [1, 2, 3, 4, 5]
    assert _count_messages(conv_id) == 5


def test_duplicate_leaves_next_seq_unchanged(
    client: TestClient, device: tuple[str, str]
) -> None:
    """重发不能把 next_seq 顶上去 —— 否则会话里会出现"空缺的 seq"。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    ).json()["id"]

    payload = {"client_msg_id": "c-x", "text": "只发一次", "generate": False}
    client.post(f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
    client.post(f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers)

    detail = client.get(f"/v1/conversations/{conv_id}", headers=headers).json()
    assert detail["next_seq"] == 1


def test_empty_text_rejected(client: TestClient, device: tuple[str, str]) -> None:
    """空消息直接拒，别在库里留一堆空行。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    ).json()["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "c-empty", "text": "   "},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
