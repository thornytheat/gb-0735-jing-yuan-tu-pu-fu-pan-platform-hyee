import json

from app.manifest import canonical
from app.modules.dispatch.plan import DEFAULT_CAPACITY, build_plan, source_digest


def _publish(client, base, capacity=4):
    return client.post(f"{base}/dispatch?capacity={capacity}")


def _mutate_snapshot(client, run_id, mutate):
    store = client.app.state.runs.store
    manifest, snapshot, pinned = store.get(run_id)
    changed = json.loads(json.dumps(snapshot))
    mutate(changed)
    with store.connect() as connection:
        connection.execute("UPDATE wafer_runs SET snapshot = ? WHERE run_id = ?", (canonical(changed), run_id))
    return manifest, changed


def test_preview_places_four_slots_across_wafers_and_lists_coverage(client, run_base):
    data = client.get(run_base + "/dispatch").json()
    plan = data["plan"]
    assert plan["capacity"] == DEFAULT_CAPACITY == 4 and plan["filled"] == 4
    assert plan["publishable"] is True and plan["blockers"] == []
    slots = plan["slots"]
    assert [slot["slot"] for slot in slots] == [1, 2, 3, 4]
    assert len({(slot["wafer"], slot["x"], slot["y"]) for slot in slots}) == 4
    assert {slot["wafer"] for slot in slots} == {"W-07", "W-08"}
    assert sum(slot["basis"] == "WAFER_FLOOR" for slot in slots) == 2
    assert all(slot["history"] and slot["history"][-1]["result"] == "FAIL" for slot in slots)

    wafers = {row["wafer"]: row for row in plan["wafers"]}
    assert wafers["W-07"]["covered"] and wafers["W-08"]["covered"]
    assert sum(row["slots"] for row in plan["wafers"]) == 4
    covered = [cluster for cluster in plan["clusters"] if cluster["covered"]]
    assert len(covered) == 4
    assert sum(len(cluster["slots"]) for cluster in covered) == 4
    assert plan["uncovered_wafers"] == []
    assert len(plan["uncovered_clusters"]) == len(plan["clusters"]) - 4


def test_same_run_republish_and_rerun_reuse_single_dispatch(client, run_base):
    first = _publish(client, run_base)
    assert first.status_code == 201 and first.json()["reused"] is False
    dispatch = first.json()["dispatch"]
    assert dispatch["state"] == "CURRENT" and dispatch["plan"]["filled"] == 4

    repeat = _publish(client, run_base)
    assert repeat.status_code == 200 and repeat.json()["reused"] is True
    assert repeat.json()["dispatch"]["dispatch_id"] == dispatch["dispatch_id"]

    assert client.post(run_base + "/rerun").json()["verified"] is True
    after_rerun = _publish(client, run_base)
    assert after_rerun.status_code == 200 and after_rerun.json()["reused"] is True
    history = client.get(run_base + "/dispatch/history/list").json()
    assert len(history) == 1 and history[0]["dispatch_id"] == dispatch["dispatch_id"]


def test_upstream_cluster_change_marks_old_dispatch_stale_and_recomputes(client, run_base):
    old = _publish(client, run_base).json()["dispatch"]
    before = client.get(run_base + "/dispatch").json()
    assert before["existing"]["dispatch_id"] == old["dispatch_id"]

    _mutate_snapshot(client, old["run_id"],
                     lambda snapshot: snapshot["clusters"].__setitem__("W-08", snapshot["clusters"]["W-08"][:1]))

    preview = client.get(run_base + "/dispatch").json()
    assert preview["existing"] is None
    assert [row["state"] for row in preview["history"]] == ["STALE"]
    assert preview["plan"]["data_sha256"] != old["plan"]["data_sha256"]
    assert client.get(f"{run_base}/dispatch/{old['dispatch_id']}/export").json()["code"] == "DISPATCH_STALE"

    fresh = _publish(client, run_base)
    assert fresh.status_code == 201 and fresh.json()["dispatch"]["dispatch_id"] != old["dispatch_id"]
    history = client.get(run_base + "/dispatch/history/list").json()
    assert [row["state"] for row in history] == ["STALE", "CURRENT"]
    assert len([row for row in history if row["state"] == "CURRENT"]) == 1


def test_audit_change_invalidates_and_blocks_publish_keeping_old_record(client, run_base):
    old = _publish(client, run_base).json()["dispatch"]

    def break_audit(snapshot):
        snapshot["audit"]["status"] = "UNBALANCED"
        snapshot["audit"]["wafers"][0]["balanced"] = False

    _mutate_snapshot(client, old["run_id"], break_audit)
    preview = client.get(run_base + "/dispatch").json()
    assert preview["plan"]["publishable"] is False
    assert {item["code"] for item in preview["plan"]["blockers"]} == {"AUDIT_UNBALANCED"}

    rejected = _publish(client, run_base)
    assert (rejected.status_code, rejected.json()["code"]) == (409, "PLAN_BLOCKED")
    history = client.get(run_base + "/dispatch/history/list").json()
    assert len(history) == 1 and history[0]["dispatch_id"] == old["dispatch_id"]
    assert history[0]["state"] == "STALE"


def test_publish_rejects_capacity_that_leaves_a_failing_wafer_without_evidence(client, run_base):
    preview = client.get(run_base + "/dispatch?capacity=1").json()
    plan = preview["plan"]
    assert plan["filled"] == 1 and plan["capacity"] == 1
    assert plan["publishable"] is False
    assert plan["uncovered_wafers"] == ["W-08"]
    assert {item["code"] for item in plan["blockers"]} == {"WAFER_UNCOVERED"}
    rejected = _publish(client, run_base, capacity=1)
    assert (rejected.status_code, rejected.json()["code"]) == (409, "PLAN_BLOCKED")
    assert client.get(run_base + "/dispatch/history/list").json() == []


def test_single_cluster_share_cap_reports_uncovered_clusters_as_warning(client, run_base):
    # 8 位次仍会漏掉部分缺陷带：这是提示，不阻断发布。
    preview = client.get(run_base + "/dispatch?capacity=8").json()
    plan = preview["plan"]
    assert plan["publishable"] is True
    assert plan["uncovered_wafers"] == []
    assert plan["uncovered_clusters"]
    assert {item["code"] for item in plan["warnings"]} == {"CLUSTER_UNCOVERED"}
    assert _publish(client, run_base, capacity=8).status_code == 201


def test_wafer_floor_guarantees_both_wafers_with_two_slots(client, run_base):
    plan = client.get(run_base + "/dispatch?capacity=2").json()["plan"]
    assert plan["publishable"] is True and plan["filled"] == 2
    assert [slot["basis"] for slot in plan["slots"]] == ["WAFER_FLOOR", "WAFER_FLOOR"]
    assert {slot["wafer"] for slot in plan["slots"]} == {"W-07", "W-08"}


def test_baseline_without_failing_cells_cannot_publish(client):
    started = client.post("/api/runs", json={"sample": "baseline"}).json()["manifest"]
    base = f'/api/runs/{started["run_id"]}'
    preview = client.get(base + "/dispatch").json()
    assert preview["plan"]["slots"] == [] and preview["plan"]["publishable"] is False
    assert {item["code"] for item in preview["plan"]["blockers"]} == {"NO_CANDIDATES"}
    rejected = _publish(client, base)
    assert (rejected.status_code, rejected.json()["code"]) == (409, "PLAN_BLOCKED")


def test_plan_is_deterministic_for_same_upstream(package, snapshot):
    manifest = {"run_id": "fixed", "lot": "FT26-1120", "wafers": ["W-07", "W-08"]}
    first = build_plan(manifest, snapshot, 4)
    second = build_plan(manifest, snapshot, 4)
    assert canonical(first) == canonical(second)
    assert source_digest(snapshot) == first["data_sha256"]


def test_published_dispatch_exports_current_version(client, run_base):
    dispatch = _publish(client, run_base).json()["dispatch"]
    response = client.get(f"{run_base}/dispatch/{dispatch['dispatch_id']}/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    data = response.json()
    assert data["format"] == "wafer-dispatch-v1"
    assert data["dispatch_id"] == dispatch["dispatch_id"]
    assert data["plan"]["slots"] == dispatch["plan"]["slots"]
    assert client.get(f"{run_base}/dispatch/missing").json()["code"] == "DISPATCH_NOT_FOUND"


def test_new_run_for_same_lot_does_not_open_second_dispatch(client, run_base, package):
    from app.modules.intake.contract import InputPackage

    service = client.app.state.runs
    dispatch = _publish(client, run_base).json()["dispatch"]
    original = service.loader

    def relabeled(sample):
        loaded = original(sample)
        return InputPackage(loaded.spec.model_copy(update={"label": "复判混合样例（改版）"}), loaded.files)

    service.loader = relabeled
    try:
        revised = client.post("/api/runs", json={"sample": "mixed"}).json()["manifest"]
    finally:
        service.loader = original
    assert revised["lot"] == dispatch["lot"] and revised["run_id"] != dispatch["run_id"]

    preview = client.get(f'/api/runs/{revised["run_id"]}/dispatch').json()
    assert preview["existing"] is None or preview["existing"]["dispatch_id"] == dispatch["dispatch_id"]
    history = client.get(f'/api/runs/{revised["run_id"]}/dispatch/history/list').json()
    assert [row["dispatch_id"] for row in history] == [dispatch["dispatch_id"]]
    assert len([row for row in history if row["state"] == "CURRENT"]) == 1
