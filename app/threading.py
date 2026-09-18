"""会话归并的纯逻辑。

父节点确定规则：
    1. 优先采用有效 in_reply_to（指向已知邮件，且不指向自身）；
    2. 否则从右向左扫描 references，取其中最后一个“已知项”；
    3. 都无法命中则自成根节点（断链邮件）。

引用环处理：
    候选父子关系构成一张“功能图”（每封新邮件至多一个候选父节点）。
    检测到引用环时，环上所有邮件整体标记为 invalid，绝不下沉、
    绝不部分挂接；随后让依赖该环的邮件重新在其引用中寻找其它有效
    父节点，重复检测直至没有环。指向历史库中已归档邮件的边不可能
    成环（历史邮件的父指针不可变）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class ThreadMessage:
    """参与会话归并的一封邮件。"""

    message_id: str
    in_reply_to: Optional[str]
    references: tuple[str, ...]
    sent_at: str          # 统一为 UTC ISO 8601 字符串，便于稳定排序
    subject: str


@dataclass(frozen=True)
class Duplicate:
    """重复 message_id 诊断。"""

    message_id: str
    first_submitted_seq: int
    occurrences: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class MissingRefs:
    """缺失引用诊断：列出该邮件引用中、已知集合里不存在的去重 ID。"""

    message_id: str
    missing: list[str]


@dataclass
class BatchResolution:
    """一批邮件归并后的完整结论。

    parent_of 仅包含最终有效挂接；环上邮件被整体剔除（invalid），
    不出现在 parent_of 中，也不挂到任何根节点下。
    """

    first_seen: dict[str, ThreadMessage]
    duplicates: list[Duplicate]
    missing_refs: list[MissingRefs]
    invalid_ids: set[str]
    cycles: list[list[str]]
    parent_of: dict[str, Optional[str]]
    roots: list[str]


def _pick_candidate(
    msg: ThreadMessage,
    known_ids: set[str],
    invalid_ids: set[str],
) -> Optional[str]:
    """按优先级挑选候选父节点。

    - in_reply_to 有效（已知、非当前 invalid 环成员）则采用；
    - 否则取 references 中从右往左第一个满足条件的已知项；
    - 否则为 None（断链，自成根）。

    指向自身的引用也作为候选保留，从而形成自环，交由环检测整体判 invalid。
    """

    irt = msg.in_reply_to
    if (
        irt is not None
        and irt in known_ids
        and irt not in invalid_ids
    ):
        return irt

    for ref in reversed(msg.references):
        if ref in known_ids and ref not in invalid_ids:
            return ref
    return None


def _find_cycles(
    new_ids: set[str],
    parent_of: dict[str, Optional[str]],
) -> list[list[str]]:
    """在新邮件构成的功能子图中找出全部环。

    父节点为历史邮件或为 None 的边会穿出子图，不可能成环。
    """

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {mid: WHITE for mid in new_ids}
    cycles: list[list[str]] = []

    for start in sorted(new_ids):
        if color[start] != WHITE:
            continue
        stack: list[str] = []
        cur: Optional[str] = start
        while cur is not None and color.get(cur, BLACK) == WHITE:
            color[cur] = GRAY
            stack.append(cur)
            nxt = parent_of.get(cur)
            if nxt is None or nxt not in color:
                break  # 穿出子图
            cur = nxt
        else:
            # 以 GRAY 节点收尾 => 发现环
            if cur is not None and color.get(cur) == GRAY:
                idx = stack.index(cur)
                cycles.append(stack[idx:])

        for node in stack:
            color[node] = BLACK

    return cycles


def resolve_batch(
    submitted: Iterable[ThreadMessage],
    existing_ids: Iterable[str] = (),
) -> BatchResolution:
    """归并一批邮件。

    Args:
        submitted: 调用方按顺序提交的邮件（同批可含重复 ID）。
        existing_ids: 历史批次中已归档的 message_id。

    Returns:
        BatchResolution，所有列表均已稳定排序。
    """

    submitted_list = list(submitted)

    # ---- 1. 同批去重，记录重复诊断 -------------------------------------
    first_index: dict[str, int] = {}
    first_seen: dict[str, ThreadMessage] = {}
    occurrences: dict[str, list[int]] = {}
    for idx, msg in enumerate(submitted_list, start=1):
        mid = msg.message_id
        if mid not in first_seen:
            first_index[mid] = idx
            first_seen[mid] = msg
            occurrences[mid] = [idx]
        else:
            occurrences[mid].append(idx)

    duplicates = [
        Duplicate(
            message_id=mid,
            first_submitted_seq=first_index[mid],
            occurrences=occurrences[mid],
        )
        for mid, occ in occurrences.items()
        if len(occ) > 1
    ]
    duplicates.sort(key=lambda d: (d.first_submitted_seq, d.message_id))

    new_ids = set(first_seen)
    known_ids = set(existing_ids) | new_ids

    # ---- 2. 缺失引用诊断（一次算定，与最终是否成环无关） ----------------
    missing_refs: list[MissingRefs] = []
    for mid in sorted(new_ids):
        msg = first_seen[mid]
        seen: set[str] = set()
        missing: list[str] = []
        for ref in msg.references:
            if ref not in known_ids and ref not in seen:
                seen.add(ref)
                missing.append(ref)
        irt = msg.in_reply_to
        if irt is not None and irt not in known_ids and irt not in seen:
            seen.add(irt)
            missing.append(irt)
        if missing:
            missing.sort()
            missing_refs.append(MissingRefs(message_id=mid, missing=missing))
    missing_refs.sort(key=lambda m: m.message_id)

    # ---- 3. 候选父节点 + 定点评测：环整体 invalid，绝不部分挂接 ---------
    invalid_ids: set[str] = set()
    cycles: list[list[str]] = []
    while True:
        parent_of: dict[str, Optional[str]] = {}
        for mid, msg in first_seen.items():
            if mid in invalid_ids:
                continue
            parent_of[mid] = _pick_candidate(msg, known_ids, invalid_ids)

        found = _find_cycles(new_ids - invalid_ids, parent_of)
        if not found:
            break

        normalized: list[list[str]] = []
        for cyc in found:
            k = cyc.index(min(cyc))
            cyc = cyc[k:] + cyc[:k]
            normalized.append(cyc)
            invalid_ids.update(cyc)
        normalized.sort(key=lambda c: c)
        cycles.extend(normalized)

    cycles.sort(key=lambda c: c)

    roots = sorted(
        (mid for mid, pid in parent_of.items() if pid is None),
        key=lambda m: (first_seen[m].sent_at, m),
    )

    return BatchResolution(
        first_seen=first_seen,
        duplicates=duplicates,
        missing_refs=missing_refs,
        invalid_ids=invalid_ids,
        cycles=cycles,
        parent_of=parent_of,
        roots=roots,
    )


def _sort_key(sent_at: str, message_id: str) -> tuple[str, str]:
    return (sent_at, message_id)


def build_forest(
    messages: dict[str, ThreadMessage],
    parent_of: dict[str, Optional[str]],
) -> list[dict]:
    """把 parent_of 展开为稳定排序的会话树。

    每棵树按 (sent_at, message_id) 排序；同一父节点下的子节点、
    以及根之间都使用同样的键，保证全序稳定。
    """

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for mid, pid in parent_of.items():
        if pid is None:
            roots.append(mid)
        else:
            children.setdefault(pid, []).append(mid)

    def node_key(mid: str) -> tuple[str, str]:
        return _sort_key(messages[mid].sent_at, mid)

    roots.sort(key=node_key)
    for kids in children.values():
        kids.sort(key=node_key)

    def to_node(mid: str) -> dict:
        msg = messages[mid]
        return {
            "message_id": msg.message_id,
            "parent_id": parent_of.get(mid),
            "in_reply_to": msg.in_reply_to,
            "references": list(msg.references),
            "sent_at": msg.sent_at,
            "subject": msg.subject,
            "children": [to_node(c) for c in children.get(mid, [])],
        }

    return [to_node(r) for r in roots]
