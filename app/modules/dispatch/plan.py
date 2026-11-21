"""抽样派工单：把有限的复判位分给最终失败缺陷带，并给出覆盖缺口。

分配规则（可复核、确定性）：
1. 每片有最终失败芯粒的晶圆先保底一个复判位，避免另一片零证据；
2. 剩余位次按簇优先级分配，同簇每多占一位就折减一次，避免全压在一处；
3. 簇优先级 = 成员数 × 100 + 分档种类 × 10 + 邻接待测/冲突芯粒数，
   即优先照顾“整条带更可能翻盘”的大带与混合分档带；
4. 同簇内先取簇起点，后续位次优先拉开格距，让一条带的证据不挤在相邻芯粒。
"""

from collections import Counter
from fractions import Fraction

from app.config import RULE_VERSION
from app.manifest import canonical, digest

PLAN_VERSION = 1
DEFAULT_CAPACITY = 4
MAX_CAPACITY = 32
NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))
UNSTABLE = {"PENDING", "CONFLICT"}


def source_digest(snapshot):
    """上游缺陷簇 + 勾稽结果 + 规则/派工算法版本，决定旧单是否失效。"""
    payload = {"plan_version": PLAN_VERSION, "rule_version": RULE_VERSION,
               "clusters": snapshot["clusters"], "audit": snapshot["audit"]}
    return digest(canonical(payload).encode("utf-8"))


def _open_neighbors(points, cells):
    seen = set()
    count = 0
    for x, y in points:
        for dx, dy in NEIGHBORS:
            key = f"{x + dx},{y + dy}"
            if key in seen:
                continue
            seen.add(key)
            cell = cells.get(key)
            if cell and cell.get("result") in UNSTABLE:
                count += 1
    return count


def _cluster_records(manifest, snapshot):
    order = {wafer: index for index, wafer in enumerate(manifest["wafers"])}
    records = []
    for wafer, groups in snapshot["clusters"].items():
        cells = snapshot["decisions"].get(wafer, {})
        for index, group in enumerate(groups):
            points = [list(point) for point in group["points"]]
            bins = dict(sorted(group["bins"].items()))
            exposed = _open_neighbors(points, cells)
            records.append({
                "wafer": wafer,
                "id": group["id"],
                "wafer_index": order.get(wafer, len(order)),
                "cluster_index": index,
                "size": group["size"],
                "bins": bins,
                "anchor": list(group["anchor"]),
                "points": points,
                "open_neighbors": exposed,
                "score": group["size"] * 100 + len(bins) * 10 + min(exposed, 9),
                "taken": 0,
                "chosen": set(),
            })
    records.sort(key=lambda record: (record["wafer_index"], record["cluster_index"]))
    return records


def _pick_point(record):
    unused = [point for point in record["points"] if tuple(point) not in record["chosen"]]
    if not record["chosen"]:
        return unused[0]
    best = None
    for point in unused:
        gap = min(max(abs(point[0] - other[0]), abs(point[1] - other[1])) for other in record["chosen"])
        if best is None or gap > best[0]:
            best = (gap, point)
    return best[1]


def _allocate(records, capacity, snapshot):
    slots = []

    def take(record, basis):
        point = _pick_point(record)
        record["chosen"].add(tuple(point))
        record["taken"] += 1
        cell = snapshot["decisions"].get(record["wafer"], {}).get(f"{point[0]},{point[1]}", {})
        slots.append({
            "wafer": record["wafer"],
            "x": point[0],
            "y": point[1],
            "cluster": record["id"],
            "bin": cell.get("bin"),
            "attempt": cell.get("attempt"),
            "history": cell.get("history", []),
            "sources": cell.get("sources", []),
            "basis": basis,
            "priority": record["score"],
            "_wafer_index": record["wafer_index"],
            "_cluster_index": record["cluster_index"],
            "_point_index": record["points"].index(point),
        })

    by_wafer = {}
    for record in records:
        by_wafer.setdefault(record["wafer"], []).append(record)

    for wafer in sorted(by_wafer, key=lambda name: by_wafer[name][0]["wafer_index"]):
        if len(slots) >= capacity:
            break
        best = min(by_wafer[wafer], key=lambda record: (-record["score"], record["cluster_index"]))
        take(best, "WAFER_FLOOR")

    while len(slots) < capacity:
        options = [record for record in records if record["taken"] < record["size"]]
        if not options:
            break
        best = min(options, key=lambda record: (-Fraction(record["score"], 1 + record["taken"]),
                                                record["wafer_index"], record["cluster_index"]))
        take(best, "PRIORITY")

    slots.sort(key=lambda slot: (slot["_wafer_index"], slot["_cluster_index"], slot["_point_index"]))
    for index, slot in enumerate(slots, 1):
        slot["slot"] = index
        for private in ("_wafer_index", "_cluster_index", "_point_index"):
            slot.pop(private)
    return slots


def _audit_unbalanced(audit):
    if audit.get("status") == "UNBALANCED":
        return True
    if not audit.get("rows", {}).get("balanced", True):
        return True
    return any(not wafer.get("balanced", True) for wafer in audit.get("wafers", []))


def _blockers(snapshot, records, uncovered_wafers):
    blockers = []
    if _audit_unbalanced(snapshot["audit"]):
        blockers.append({"code": "AUDIT_UNBALANCED", "message": "勾稽失衡：阵列、行数或台账未对齐，不能发布派工单"})
    if not records:
        blockers.append({"code": "NO_CANDIDATES", "message": "当前图批没有最终失败芯粒，无需派工"})
    if uncovered_wafers:
        blockers.append({"code": "WAFER_UNCOVERED",
                         "message": f"复判位不足，{'、'.join(uncovered_wafers)} 会零证据，需增加位次或先确认勾稽"})
    return blockers


def _warnings(records):
    warnings = []
    if any(record["size"] > record["taken"] for record in records):
        warnings.append({"code": "CLUSTER_UNCOVERED", "message": "仍有缺陷带整带未取得复判依据"})
    return warnings


def build_plan(manifest, snapshot, capacity=DEFAULT_CAPACITY):
    capacity = max(1, min(int(capacity), MAX_CAPACITY))
    records = _cluster_records(manifest, snapshot)
    slots = _allocate(records, capacity, snapshot)
    covered = {}
    for slot in slots:
        covered.setdefault(slot["wafer"], set()).add(slot["cluster"])

    wafers = []
    for wafer in manifest["wafers"]:
        groups = [record for record in records if record["wafer"] == wafer]
        used = covered.get(wafer, set())
        wafers.append({
            "wafer": wafer,
            "failing": sum(record["size"] for record in groups),
            "clusters": len(groups),
            "slots": sum(1 for slot in slots if slot["wafer"] == wafer),
            "covered_clusters": [record["id"] for record in groups if record["id"] in used],
            "uncovered_clusters": [record["id"] for record in groups if record["id"] not in used],
            "covered": bool(used),
        })

    clusters = []
    for record in records:
        used = sorted(slot["slot"] for slot in slots
                      if slot["wafer"] == record["wafer"] and slot["cluster"] == record["id"])
        clusters.append({
            "wafer": record["wafer"], "id": record["id"], "size": record["size"],
            "bins": record["bins"], "anchor": record["anchor"],
            "open_neighbors": record["open_neighbors"], "priority": record["score"],
            "covered": bool(used), "slots": used,
        })

    uncovered_wafers = [wafer["wafer"] for wafer in wafers if wafer["failing"] and not wafer["covered"]]
    uncovered_clusters = [{"wafer": record["wafer"], "id": record["id"], "size": record["size"]}
                          for record in records if record["id"] not in covered.get(record["wafer"], set())]
    blockers = _blockers(snapshot, records, uncovered_wafers)
    warnings = _warnings(records)
    return {
        "schema": "wafer-dispatch-v1",
        "plan_version": PLAN_VERSION,
        "lot": manifest["lot"],
        "rule_version": RULE_VERSION,
        "data_sha256": source_digest(snapshot),
        "capacity": capacity,
        "filled": len(slots),
        "unfilled": max(0, capacity - len(slots)),
        "slots": slots,
        "wafers": wafers,
        "clusters": clusters,
        "uncovered_wafers": uncovered_wafers,
        "uncovered_clusters": uncovered_clusters,
        "blockers": blockers,
        "warnings": warnings,
        "publishable": not blockers,
    }


def reason_counts(plan):
    return dict(Counter(slot["basis"] for slot in plan["slots"]))
