"""端到端 API 测试：提交、跨批归并、诊断、查询。"""

from __future__ import annotations


def _m(mid, irt=None, refs=None, sent="2026-09-18T10:00:00+00:00", subject="hello"):
    return {
        "message_id": mid,
        "in_reply_to": irt,
        "references": refs or [],
        "sent_at": sent,
        "subject": subject,
    }


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_submit_and_get_batch_snapshot(client):
    payload = {"messages": [
        _m("A", sent="2026-09-18T09:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00"),
    ]}
    r = client.post("/batches", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["totals"]["submitted"] == 2
    assert body["totals"]["trees"] == 1

    # 快照查询结果一致
    again = client.get(f"/batches/{body['batch_id']}").json()
    assert again == body

    listed = client.get("/batches").json()
    assert listed["total"] == 1
    assert listed["batches"][0]["batch_id"] == body["batch_id"]


def test_missing_references_and_broken_chain(client):
    r = client.post("/batches", json={"messages": [
        _m("X", irt="nope", refs=["g1", "g2"]),
    ]}).json()

    assert r["threads"][0]["message_id"] == "X"
    assert r["threads"][0]["parent_id"] is None
    assert r["missing_references"] == [
        {"message_id": "X", "missing": ["g1", "g2", "nope"]}
    ]


def test_full_cycle_invalid_and_no_partial_attach(client):
    body = client.post("/batches", json={"messages": [
        _m("a", irt="c", refs=["c"], sent="2026-09-18T01:00:00+00:00"),
        _m("b", irt="a", refs=["c", "a"], sent="2026-09-18T02:00:00+00:00"),
        _m("c", irt="b", refs=["c", "a", "b"], sent="2026-09-18T03:00:00+00:00"),
    ]}).json()

    assert body["threads"] == []
    assert body["totals"]["invalid"] == 3
    assert body["totals"]["cycles"] == 1
    cyc = body["cycles"][0]
    assert set(cyc["members"]) == {"a", "b", "c"}
    assert sorted(i["message_id"] for i in body["invalid_messages"]) == ["a", "b", "c"]

    # 环邮件查询：消息存在但线程接口返回 409
    got = client.get("/messages/a").json()
    assert got["is_invalid"] is True
    assert got["parent_id"] is None
    resp = client.get("/threads/a")
    assert resp.status_code == 409


def test_duplicate_ids_in_batch(client):
    body = client.post("/batches", json={"messages": [
        _m("A"),
        _m("D", irt="A", sent="2026-09-18T11:00:00+00:00"),
        _m("D", irt=None, refs=[], sent="2026-09-18T12:00:00+00:00"),
    ]}).json()

    dups = body["duplicates"]
    assert len(dups) == 1
    assert dups[0]["message_id"] == "D"
    assert dups[0]["archived"] is False
    assert dups[0]["occurrences"] == [2, 3]
    assert body["totals"]["submitted"] == 3
    assert body["totals"]["unique_new"] == 2


def test_cross_batch_threading_and_archived_duplicate(client):
    b1 = client.post("/batches", json={"messages": [_m("A")]}).json()
    b2 = client.post("/batches", json={"messages": [
        _m("B", irt="A", sent="2026-09-18T11:00:00+00:00"),
        _m("A"),  # 已归档重复
    ]}).json()

    # B 跨批挂到历史 A 之下
    tree = b2["threads"][0]
    assert tree["message_id"] == "A"
    assert [c["message_id"] for c in tree["children"]] == ["B"]

    assert b2["duplicates"] == [{
        "message_id": "A",
        "archived": True,
        "first_submitted_seq": None,
        "occurrences": [2],
    }]
    assert b2["totals"]["archived_duplicates"] == 1

    # 线程查询返回完整跨批树
    t = client.get("/threads/B").json()
    assert t["root_id"] == "A"
    assert t["tree"]["children"][0]["message_id"] == "B"


def test_cycle_tail_falls_back_to_valid_reference(client):
    body = client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T00:00:00+00:00"),
        _m("x", irt="y", refs=["y"], sent="2026-09-18T01:00:00+00:00"),
        _m("y", irt="x", refs=["x"], sent="2026-09-18T02:00:00+00:00"),
        _m("z", irt="y", refs=["R"], sent="2026-09-18T03:00:00+00:00"),
    ]}).json()

    # x/y 整环 invalid；z 回退挂到 R
    assert body["totals"]["invalid"] == 2
    tree = body["threads"][0]
    assert tree["message_id"] == "R"
    assert [c["message_id"] for c in tree["children"]] == ["z"]


def test_message_not_found(client):
    assert client.get("/messages/zzz").status_code == 404
    assert client.get("/threads/zzz").status_code == 404
    assert client.get("/batches/zzz").status_code == 404


def test_empty_batch_rejected(client):
    assert client.post("/batches", json={"messages": []}).status_code == 422
