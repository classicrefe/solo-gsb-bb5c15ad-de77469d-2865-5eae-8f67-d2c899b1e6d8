"""端到端 API 测试：提交、跨批归并、诊断、查询。"""

from __future__ import annotations

import os
import sqlite3


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


# ---------------------------------------------------------------------------
# POST /relinks：迟到邮件归档后修复断链
# ---------------------------------------------------------------------------

def test_relink_after_late_parent_arrived(client):
    # 首批：D 因父邮件迟到而断链自成根
    b1 = client.post("/batches", json={"messages": [
        _m("D", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()
    assert b1["threads"][0]["message_id"] == "D"
    assert b1["threads"][0]["parent_id"] is None

    # 迟到的 A 归档
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T09:00:00+00:00"),
    ]})

    # 修复 D
    r = client.post("/relinks", json={"message_ids": ["D"]})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["updated"] == 1
    item = body["results"][0]
    assert item == {
        "message_id": "D",
        "old_parent_id": None,
        "new_parent_id": "A",
        "old_root_id": "D",
        "new_root_id": "A",
        "updated": True,
    }

    # /threads 随即反映新树
    t = client.get("/threads/D").json()
    assert t["root_id"] == "A"
    assert t["tree"]["message_id"] == "A"
    assert [c["message_id"] for c in t["tree"]["children"]] == ["D"]
    assert client.get("/messages/D").json()["parent_id"] == "A"

    # 既有批次结果快照保持不变（仍显示 D 为根）
    snapshot = client.get(f"/batches/{b1['batch_id']}").json()
    assert snapshot == b1


def test_relink_batch_fallback_skip_and_dedup(client):
    # A：可经 in_reply_to 修复；B：in_reply_to 仍缺失，references 回退命中 R；
    # C：无任何候选，保持不变；Y：已有父节点，跳过
    client.post("/batches", json={"messages": [
        _m("X", sent="2026-09-18T08:30:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00"),
        _m("B", irt="ghost", refs=["ghost2", "R"], sent="2026-09-18T10:00:00+00:00"),
        _m("C", irt="ghost", refs=["ghost3"], sent="2026-09-18T11:00:00+00:00"),
        _m("Y", irt="X", refs=["X"], sent="2026-09-18T12:00:00+00:00"),
    ]})
    # R 迟到归档，A/B 此时具备修复候选
    client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
    ]})

    body = client.post("/relinks", json={
        "message_ids": ["A", "B", "C", "A", "Y"]
    }).json()
    assert body["total"] == 4  # 重复 A 去重
    assert body["updated"] == 2

    by_id = {item["message_id"]: item for item in body["results"]}
    assert by_id["A"]["new_parent_id"] == "R"
    assert by_id["A"]["new_root_id"] == "R"
    assert by_id["B"]["new_parent_id"] == "R"
    assert by_id["C"] == {
        "message_id": "C",
        "old_parent_id": None,
        "new_parent_id": None,
        "old_root_id": "C",
        "new_root_id": "C",
        "updated": False,
    }
    assert by_id["Y"]["updated"] is False  # 已有父节点，原样返回
    assert by_id["Y"]["old_root_id"] == "X"

    # 重复执行是幂等的：没有新的变更
    again = client.post("/relinks", json={"message_ids": ["A", "B"]}).json()
    assert again["updated"] == 0
    assert all(i["updated"] is False for i in again["results"])


def test_relink_moves_descendant_root_and_merges_trees(client):
    # P 为断链根，C 挂在 P 下；迟到的 A 修复后整棵子树并入 A
    client.post("/batches", json={"messages": [
        _m("P", irt="A", refs=["A"], sent="2026-09-18T10:00:00+00:00"),
        _m("C", irt="P", refs=["A", "P"], sent="2026-09-18T11:00:00+00:00"),
    ]})
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T09:00:00+00:00"),
    ]})

    body = client.post("/relinks", json={"message_ids": ["P", "C"]}).json()
    by_id = {i["message_id"]: i for i in body["results"]}
    assert by_id["P"] == {
        "message_id": "P",
        "old_parent_id": None,
        "new_parent_id": "A",
        "old_root_id": "P",
        "new_root_id": "A",
        "updated": True,
    }
    # C 自身不变，但其所属根随父树迁移
    assert by_id["C"]["updated"] is False
    assert by_id["C"]["new_parent_id"] == "P"
    assert by_id["C"]["old_root_id"] == "P"
    assert by_id["C"]["new_root_id"] == "A"

    t = client.get("/threads/C").json()
    assert [n["message_id"] for n in [t["tree"]]] == ["A"]


def test_relink_409_on_cycle_rolls_back(client, tmp_path, monkeypatch):
    # 构造：P 因 Q 迟到而断链；Q 之后归档。人为把 Q 置于 P 子树内，
    # 使补挂 P->Q 形成环。正常归档流程不会出现这种状态，这里只验证
    # “合并后统一检测环、成环整批 409 且不写库”的保证。
    client.post("/batches", json={"messages": [
        _m("P", irt="Q", refs=["Q"], sent="2026-09-18T10:00:00+00:00"),
    ]})
    client.post("/batches", json={"messages": [
        _m("Q", sent="2026-09-18T09:00:00+00:00"),
    ]})

    # 直接改库：Q 挂到 P 之下（P 仍为 NULL 根）
    db_path = os.environ["DATABASE_PATH"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE messages SET parent_id = 'P' WHERE message_id = 'Q'")
        conn.commit()
    finally:
        conn.close()

    r = client.post("/relinks", json={"message_ids": ["P"]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert set(detail["cycles"][0]) == {"P", "Q"}

    # 不写库：P 仍为断链根，/threads 仍可正常查询
    assert client.get("/messages/P").json()["parent_id"] is None
    t = client.get("/threads/P").json()
    assert t["root_id"] == "P"


def test_relink_unknown_invalid_and_empty(client):
    client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"], sent="2026-09-18T01:00:00+00:00"),
        _m("y", irt="x", refs=["x"], sent="2026-09-18T02:00:00+00:00"),
        _m("A", sent="2026-09-18T03:00:00+00:00"),
    ]})

    # 未知 ID
    r = client.post("/relinks", json={"message_ids": ["A", "nope"]})
    assert r.status_code == 404
    assert r.json()["detail"]["message_ids"] == ["nope"]
    # 404 不写库：A 不受影响
    assert client.get("/messages/A").json()["parent_id"] is None

    # 环内 invalid 邮件不可修复
    r = client.post("/relinks", json={"message_ids": ["x", "A"]})
    assert r.status_code == 422
    assert r.json()["detail"]["message_ids"] == ["x"]

    # 空列表
    assert client.post("/relinks", json={"message_ids": []}).status_code == 422
    assert client.post("/relinks", json={"message_ids": ["  "]}).status_code == 422
