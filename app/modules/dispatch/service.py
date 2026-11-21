from datetime import datetime, timezone

from app.errors import ReviewError
from app.modules.dispatch.plan import DEFAULT_CAPACITY, PLAN_VERSION, build_plan, reason_counts, source_digest
from app.manifest import canonical, digest

PUBLISHED = "PUBLISHED"
SUPERSEDED = "SUPERSEDED"


def plan_identity(lot, capacity, data_sha256):
    """按图批而非运行号取身份：同一图批重跑、或契约仅改标签生成新运行号时，都不新开单据。"""
    return digest(canonical([lot, capacity, PLAN_VERSION, data_sha256]).encode("utf-8"))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state(record, data_sha256):
    if record["data_sha256"] != data_sha256:
        return "STALE"
    if record["status"] == SUPERSEDED:
        return "SUPERSEDED"
    return "CURRENT"


def _decorate(record, data_sha256, plan):
    return {
        "dispatch_id": record["dispatch_id"],
        "run_id": record["run_id"],
        "lot": record["lot"],
        "capacity": record["capacity"],
        "created_at": record["created_at"],
        "state": _state(record, data_sha256),
        "data_sha256": record["data_sha256"],
        "plan": plan,
    }


class DispatchService:
    """派工单发布与失效判定。

    - 单据键由 图批 + 位次数 + 派工算法版本 + 上游摘要 组成，重跑同一图批只会复用同一单；
    - 上游缺陷簇或勾稽摘要变化时，旧单自动判为 STALE，需重新发布；
    - 发布失败不落库，旧版单据原样保留。
    """

    def __init__(self, store):
        self.store = store

    def _context(self, run_id):
        manifest, snapshot, pinned = self.store.get(run_id)
        return manifest, snapshot, source_digest(snapshot)

    def preview(self, run_id, capacity=DEFAULT_CAPACITY):
        manifest, snapshot, data_sha256 = self._context(run_id)
        plan = build_plan(manifest, snapshot, capacity)
        dispatch_id = plan_identity(manifest["lot"], plan["capacity"], data_sha256)
        history = self.store.dispatches(manifest["lot"])
        current = next((record for record in history
                        if record["dispatch_id"] == dispatch_id and record["status"] == PUBLISHED), None)
        return {
            "plan": plan,
            "reasons": reason_counts(plan),
            "existing": {**_decorate(current, data_sha256, plan),
                         "published_at": current["created_at"]} if current else None,
            "history": [_decorate(record, data_sha256, record["plan"]) for record in history],
        }

    def publish(self, run_id, capacity=DEFAULT_CAPACITY):
        manifest, snapshot, data_sha256 = self._context(run_id)
        plan = build_plan(manifest, snapshot, capacity)
        if not plan["publishable"]:
            codes = "、".join(item["code"] for item in plan["blockers"])
            raise ReviewError("PLAN_BLOCKED", f"派工单不能发布：{codes}", 409)
        dispatch_id = plan_identity(manifest["lot"], plan["capacity"], data_sha256)
        record, created = self.store.publish_dispatch(
            dispatch_id, run_id, manifest["lot"], plan["capacity"], data_sha256, PLAN_VERSION,
            PUBLISHED, plan, _now())
        if record["plan"] != plan:
            raise ReviewError("DISPATCH_DIVERGED", "相同输入生成了不同派工单", 409)
        return {"dispatch": _decorate(record, data_sha256, plan), "reused": not created}

    def list(self, run_id):
        manifest, snapshot, data_sha256 = self._context(run_id)
        return [_decorate(record, data_sha256, record["plan"]) for record in self.store.dispatches(manifest["lot"])]

    def get(self, run_id, dispatch_id):
        manifest, snapshot, data_sha256 = self._context(run_id)
        record = self.store.dispatch(manifest["lot"], dispatch_id)
        return _decorate(record, data_sha256, record["plan"])
