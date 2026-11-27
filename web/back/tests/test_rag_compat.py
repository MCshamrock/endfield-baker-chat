"""tests/test_rag_compat.py —— 老检索服务 `/retrieve` 的兼容出口。

守三件事：
  1. 两套字段都要给（老后端读 `passages`，老网页/APK 读 `results`）；
  2. 分数落在老客户端认的区间（0.40~0.99，默认阈值 0.4 不会被全部滤掉）；
  3. 排序和聊天里预取一致（标题整串命中优先），`top_k` 生效、同一标题不重复占位。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.clock import utcnow
from app.db import SessionLocal
from app.models import WorldbookEntry
from app.repos import worldbook as worldbook_repo

ENTRIES = [
    # (source_id, title, kind, priority, keys, body)
    ("e0001", "超域", "term", "high", ["超域"], "超域是对深度读数为 1 的时空的统称。"),
    ("e0002", "超域试验场", "term", "high", ["超域试验场"], "超域试验场是联盟工团在供能高地搭设的试验场所。"),
    ("e0003", "四号谷地", "term", "high", ["四号谷地"], "四号谷地是联盟工团选中的生产开拓地区。"),
    ("e0004", "四号谷地", "term", "high", ["四号谷地"], "四号谷地在一场惨烈的灾难后失去了源石大树。"),
    ("e0005", "刀剑无名·其一", "event", "mid", ["刀剑无名·其一", "刀剑无名"], "言：目前可以先记录位置。"),
]


@pytest.fixture(autouse=True)
def seed_worldbook() -> None:
    """给这一组用例铺 5 条条目（测完删掉 + 清缓存）。"""
    with SessionLocal() as db:
        db.execute(delete(WorldbookEntry))
        for source_id, title, kind, priority, keys, body in ENTRIES:
            db.add(
                WorldbookEntry(
                    world_slug="endfield",
                    source_id=source_id,
                    title=title,
                    kind=kind,
                    priority=priority,
                    keys=keys,
                    fuzzy=[],
                    semantic=body[:120],
                    body=body,
                    is_constant=0,
                    updated_at=utcnow(),
                )
            )
        db.commit()
    worldbook_repo.clear_cache()
    yield
    with SessionLocal() as db:
        db.execute(delete(WorldbookEntry))
        db.commit()
    worldbook_repo.clear_cache()


def test_retrieve_returns_both_shapes(client: TestClient) -> None:
    body = client.post("/retrieve", json={"query": "超域试验场在哪里", "top_k": 3}).json()
    assert body["count"] == len(body["results"]) == len(body["passages"])
    assert body["model"] == "worldbook-algo"
    assert body["results"][0]["title"] == "超域试验场", "标题整串命中要排前面"
    assert body["passages"][0]["title"] == "超域试验场"
    # 两套字段各自的形状
    assert set(body["results"][0]) >= {"rank", "score", "title", "category", "text"}
    assert set(body["passages"][0]) >= {"id", "doc_id", "category", "title", "text"}
    assert body["passages"][0]["doc_id"].startswith("archive/超域试验场.md#")
    assert body["passages"][0]["text"].startswith("【超域试验场】")
    # 分数：最高 0.99，且都 ≥ 0.40（老客户端的默认阈值）
    scores = [item["score"] for item in body["results"]]
    assert scores[0] == 0.99
    assert all(0.40 <= score <= 0.99 for score in scores)


def test_retrieve_caps_chunks_per_title(client: TestClient) -> None:
    """四号谷地有两块语料：两块都给可以（老 RAG 就这样），但不能一篇霸屏。"""
    body = client.post("/retrieve", json={"query": "四号谷地", "top_k": 6}).json()
    titles = [item["title"] for item in body["results"]]
    assert titles.count("四号谷地") == 2


def test_retrieve_can_return_several_titles(client: TestClient) -> None:
    """短标题命中时，前缀更长的另一篇也要能补进来（按分数排）。"""
    body = client.post("/retrieve", json={"query": "超域试验场", "top_k": 4}).json()
    titles = [item["title"] for item in body["results"]]
    assert titles[0] == "超域试验场"
    assert "超域" in titles


def test_retrieve_does_not_pad_with_irrelevant(client: TestClient) -> None:
    """只命中一篇就只给这一篇 —— 不要拿不相关的条目凑数。"""
    body = client.post("/retrieve", json={"query": "四号谷地是什么", "top_k": 6}).json()
    assert {item["title"] for item in body["results"]} == {"四号谷地"}


def test_retrieve_respects_top_k_and_alias(client: TestClient) -> None:
    one = client.post("/retrieve", json={"query": "四号谷地", "top_k": 1}).json()
    assert one["count"] == 1
    alias = client.post("/retrieve", json={"query": "四号谷地", "k": 2}).json()
    assert alias["count"] <= 2


def test_retrieve_empty_query(client: TestClient) -> None:
    body = client.post("/retrieve", json={"query": "   "}).json()
    assert body["count"] == 0 and body["results"] == [] and body["passages"] == []


def test_retrieve_min_score_filters(client: TestClient) -> None:
    body = client.post("/retrieve", json={"query": "超域", "top_k": 5, "min_score": 0.99}).json()
    assert body["count"] == 1, "只有最高分那条能过 0.99"


def test_retrieve_story_category(client: TestClient) -> None:
    body = client.post("/retrieve", json={"query": "刀剑无名讲了什么", "top_k": 2}).json()
    assert body["results"][0]["category"] == "story"


def test_stats_and_health(client: TestClient) -> None:
    stats = client.get("/stats").json()
    assert stats["ok"] is True and stats["mode"] == "algo"
    assert stats["entries"] == len(ENTRIES)
    # 老 rag_client 的探活读的是 ok 字段
    assert client.get("/health").json().get("ok") is True
