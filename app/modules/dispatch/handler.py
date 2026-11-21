from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.config import RULE_VERSION
from app.dependencies import Service
from app.errors import ReviewError
from app.manifest import canonical, digest
from app.modules.dispatch.plan import DEFAULT_CAPACITY, MAX_CAPACITY
from app.modules.dispatch.service import DispatchService

router = APIRouter(prefix="/api/runs/{run_id}/dispatch")
Capacity = Annotated[int, Query(ge=1, le=MAX_CAPACITY)]


@router.get("")
def preview(run_id: str, request_service: Service, capacity: Capacity = DEFAULT_CAPACITY):
    return DispatchService(request_service.store).preview(run_id, capacity)


@router.post("", status_code=201)
def publish(run_id: str, request_service: Service, response: Response, capacity: Capacity = DEFAULT_CAPACITY):
    result = DispatchService(request_service.store).publish(run_id, capacity)
    if result["reused"]:
        response.status_code = 200
    return result


@router.get("/history/list")
def history(run_id: str, request_service: Service):
    return DispatchService(request_service.store).list(run_id)


@router.get("/{dispatch_id}")
def dispatch(run_id: str, dispatch_id: str, request_service: Service):
    return DispatchService(request_service.store).get(run_id, dispatch_id)


@router.get("/{dispatch_id}/export")
def export(run_id: str, dispatch_id: str, request_service: Service):
    record = DispatchService(request_service.store).get(run_id, dispatch_id)
    if record["state"] != "CURRENT":
        raise ReviewError("DISPATCH_STALE", "派工单已失效，请按当前上游重新发布", 409)
    body = canonical({
        "format": "wafer-dispatch-v1",
        "dispatch_id": record["dispatch_id"],
        "run_id": record["run_id"],
        "created_at": record["created_at"],
        "rule_version": RULE_VERSION,
        "data_sha256": record["data_sha256"],
        "summary_sha256": digest(canonical(record["plan"]).encode("utf-8")),
        "plan": record["plan"],
    }).encode("utf-8")
    filename = f"dispatch-{record['dispatch_id'][:16]}.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
