"""业务编排：调用纯归并逻辑，落库 SQLite，组装查询结果。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import database, threading
from .schemas import BatchIn
from .threading import ThreadMessage


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_message(row: sqlite3.Row) -> ThreadMessage:
    return ThreadMessage(
        message_id=row["message_id"],
        in_reply_to=row["in_reply_to"],
        references=tuple(json.loads(row["references"])),
        sent_at=row["sent_at"],
        subject=row["subject"],
    )


def _climb_root(conn: sqlite3.Connection, message_id: str) -> str:
    """沿 parent_id 向上爬到根（新邮件均为有效邮件，链不会断）。"""

    cur = message_id
    seen: set[str] = set()
    while True:
        row = conn.execute(
            "SELECT parent_id FROM messages WHERE message_id = ?", (cur,)
        ).fetchone()
        pid = row["parent_id"] if row else None
        if pid is None or pid in seen:
            return cur
        seen.add(cur)
        cur = pid


def _load_subtree(conn: sqlite3.Connection, root_id: str) -> tuple[dict[str, ThreadMessage], dict[str, Optional[str]]]:
    """递归取出某根下全部有效节点（parent_id 在树内）。"""

    rows = conn.execute(
        """
        WITH RECURSIVE tree(message_id, in_reply_to, "references", sent_at, subject, parent_id, depth) AS (
            SELECT message_id, in_reply_to, "references", sent_at, subject, parent_id, 0
            FROM messages WHERE message_id = ?
            UNION ALL
            SELECT m.message_id, m.in_reply_to, m."references", m.sent_at, m.subject, m.parent_id, t.depth + 1
            FROM messages m JOIN tree t ON m.parent_id = t.message_id
            WHERE m.is_invalid = 0
        )
        SELECT message_id, in_reply_to, "references", sent_at, subject, parent_id
        FROM tree ORDER BY depth
        """,
        (root_id,),
    ).fetchall()

    messages: dict[str, ThreadMessage] = {}
    parent_of: dict[str, Optional[str]] = {}
    for row in rows:
        mid = row["message_id"]
        messages[mid] = _row_to_message(row)
        parent_of[mid] = row["parent_id"]
    return messages, parent_of


def _build_payload(
    batch_id: str,
    submitted_at: str,
    normalized_messages: list,
    resolution: threading.BatchResolution,
    archived_duplicates: dict[str, int],
    threads: list[dict],
) -> dict:
    """组装对外响应（同时也是持久化快照）。"""

    duplicates = []
    # 已在历史批次归档的重复 ID
    for mid in sorted(archived_duplicates):
        duplicates.append({
            "message_id": mid,
            "archived": True,
            "first_submitted_seq": None,
            "occurrences": [archived_duplicates[mid]],
        })
    # 本批内的重复 ID
    for dup in resolution.duplicates:
        duplicates.append({
            "message_id": dup.message_id,
            "archived": False,
            "first_submitted_seq": dup.first_submitted_seq,
            "occurrences": dup.occurrences,
        })
    duplicates.sort(key=lambda d: (d["message_id"], 0 if d["archived"] else 1))

    missing_references = [
        {"message_id": m.message_id, "missing": m.missing}
        for m in resolution.missing_refs
    ]

    by_id = {m.message_id: m for m in normalized_messages}
    cycles_out: list[dict] = []
    invalid_messages: list[dict] = []
    for idx, cyc in enumerate(resolution.cycles, start=1):
        cycles_out.append({"index": idx, "members": list(cyc), "invalid_messages": list(cyc)})
        for mid in cyc:
            m = by_id[mid]
            invalid_messages.append({
                "message_id": mid,
                "in_reply_to": m.in_reply_to,
                "references": list(m.references),
                "sent_at": m.sent_at.isoformat(),
                "subject": m.subject,
                "reason": "reference_cycle",
                "cycle_index": idx,
            })
    invalid_messages.sort(key=lambda im: im["message_id"])

    total_count = len(normalized_messages)
    unique_count = len(resolution.first_seen)
    invalid_count = len(resolution.invalid_ids)

    return {
        "batch_id": batch_id,
        "submitted_at": submitted_at,
        "totals": {
            "submitted": total_count,
            "unique_new": unique_count,
            "archived_duplicates": len(archived_duplicates),
            "invalid": invalid_count,
            "missing_reference_messages": len(resolution.missing_refs),
            "cycles": len(resolution.cycles),
            "trees": len(threads),
        },
        "threads": threads,
        "duplicates": duplicates,
        "missing_references": missing_references,
        "cycles": cycles_out,
        "invalid_messages": invalid_messages,
    }


def submit_batch(payload: BatchIn) -> dict:
    """归并、落库、返回完整结果。同库写操作串行化。"""

    normalized = [m.normalized() for m in payload.messages]

    with database.write_lock():
        conn = database.connect()
        try:
            existing_ids = {
                r["message_id"]
                for r in conn.execute("SELECT message_id FROM messages")
            }

            # 已归档 ID 整组剔出，作为“重复 ID（历史归档）”诊断
            archived_duplicates: dict[str, int] = {}
            fresh: list = []
            for seq, m in enumerate(normalized, start=1):
                if m.message_id in existing_ids:
                    archived_duplicates.setdefault(m.message_id, seq)
                else:
                    fresh.append(m)
            thread_messages = [
                ThreadMessage(
                    message_id=m.message_id,
                    in_reply_to=m.in_reply_to,
                    references=tuple(m.references),
                    sent_at=m.sent_at.isoformat(),
                    subject=m.subject,
                )
                for m in fresh
            ]
            resolution = threading.resolve_batch(
                thread_messages, existing_ids=existing_ids
            )

            batch_id = uuid.uuid4().hex
            submitted_at = _utc_now_iso()

            # 1) 批次元数据（messages/batch_* 均以外键指向它）
            conn.execute(
                """
                INSERT INTO batches
                    (batch_id, submitted_at, total_count, unique_count, invalid_count, tree_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    submitted_at,
                    len(normalized),
                    len(resolution.first_seen),
                    len(resolution.invalid_ids),
                    0,  # 树数量在构建后回填
                ),
            )

            # 2) 写入新邮件（含 invalid；invalid 父指针置 NULL，绝不部分挂接）
            inserted_ids: set[str] = set()
            for seq, m in enumerate(normalized, start=1):
                if m.message_id in inserted_ids or m.message_id in archived_duplicates:
                    continue  # 本批重复的后续出现、历史归档重复，均不重复落库
                inserted_ids.add(m.message_id)
                is_invalid = m.message_id in resolution.invalid_ids
                parent_id = None if is_invalid else resolution.parent_of.get(m.message_id)
                conn.execute(
                    """
                    INSERT INTO messages
                        (message_id, in_reply_to, "references", sent_at, subject,
                         parent_id, is_invalid, batch_id, submitted_seq, archived_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        m.message_id,
                        m.in_reply_to,
                        json.dumps(m.references, ensure_ascii=False),
                        m.sent_at.isoformat(),
                        m.subject,
                        parent_id,
                        1 if is_invalid else 0,
                        batch_id,
                        seq,
                        submitted_at,
                    ),
                )

            # 3) 本批所有有效新邮件向上爬到根，取整棵树（含历史祖先）
            new_valid_ids = [
                mid for mid in resolution.first_seen if mid not in resolution.invalid_ids
            ]
            root_ids = {_climb_root(conn, mid) for mid in new_valid_ids}

            all_messages: dict[str, ThreadMessage] = {}
            all_parents: dict[str, Optional[str]] = {}
            for root_id in root_ids:
                msgs, parents = _load_subtree(conn, root_id)
                all_messages.update(msgs)
                all_parents.update(parents)

            threads = threading.build_forest(all_messages, all_parents)

            result_payload = _build_payload(
                batch_id,
                submitted_at,
                normalized,
                resolution,
                archived_duplicates,
                threads,
            )

            # 4) 回填树数量 + 诊断明细
            conn.execute(
                "UPDATE batches SET tree_count = ? WHERE batch_id = ?",
                (len(threads), batch_id),
            )
            for dup in result_payload["duplicates"]:
                conn.execute(
                    "INSERT INTO batch_duplicates (batch_id, message_id, archived, first_submitted_seq, occurrences) VALUES (?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        dup["message_id"],
                        1 if dup["archived"] else 0,
                        dup["first_submitted_seq"],
                        json.dumps(dup["occurrences"]),
                    ),
                )
            for mr in result_payload["missing_references"]:
                conn.execute(
                    "INSERT INTO batch_missing_refs (batch_id, message_id, missing) VALUES (?, ?, ?)",
                    (batch_id, mr["message_id"], json.dumps(mr["missing"], ensure_ascii=False)),
                )
            for cyc in result_payload["cycles"]:
                conn.execute(
                    "INSERT INTO batch_cycles (batch_id, idx, members) VALUES (?, ?, ?)",
                    (batch_id, cyc["index"], json.dumps(cyc["members"], ensure_ascii=False)),
                )

            # 5) 结果快照
            conn.execute(
                "INSERT INTO batch_results (batch_id, result_json) VALUES (?, ?)",
                (batch_id, json.dumps(result_payload, ensure_ascii=False)),
            )

            conn.commit()
            return result_payload
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def list_batches(limit: int, offset: int) -> dict:
    conn = database.connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY submitted_at DESC, batch_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items = [
            {
                "batch_id": r["batch_id"],
                "submitted_at": r["submitted_at"],
                "total_count": r["total_count"],
                "unique_count": r["unique_count"],
                "invalid_count": r["invalid_count"],
                "tree_count": r["tree_count"],
            }
            for r in rows
        ]
        return {"total": total, "limit": limit, "offset": offset, "batches": items}
    finally:
        conn.close()


def get_batch(batch_id: str) -> Optional[dict]:
    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT result_json FROM batch_results WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return json.loads(row["result_json"]) if row else None
    finally:
        conn.close()


def get_message(message_id: str) -> Optional[dict]:
    conn = database.connect()
    try:
        row = conn.execute(
            'SELECT message_id, in_reply_to, "references", sent_at, subject, parent_id, is_invalid, batch_id FROM messages WHERE message_id = ?',
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "message_id": row["message_id"],
            "in_reply_to": row["in_reply_to"],
            "references": json.loads(row["references"]),
            "sent_at": row["sent_at"],
            "subject": row["subject"],
            "parent_id": row["parent_id"],
            "is_invalid": bool(row["is_invalid"]),
            "batch_id": row["batch_id"],
        }
    finally:
        conn.close()


def get_thread(message_id: str) -> Optional[dict]:
    """返回某邮件所在会话的完整树。环邮件不挂接，返回 409。"""

    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT is_invalid FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        if row["is_invalid"]:
            return {"invalid": True, "message_id": message_id}

        root_id = _climb_root(conn, message_id)
        msgs, parents = _load_subtree(conn, root_id)
        forest = threading.build_forest(msgs, parents)
        return {"root_id": root_id, "tree": forest[0] if forest else None}
    finally:
        conn.close()
