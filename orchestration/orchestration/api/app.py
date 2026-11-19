"""API Server — 1 service, 3 vai trò, tách theo route (ORCHESTRATION_DESIGN.md mục 6).

    Lớp A (đọc):  GET /dependencies, /dependencies/{id}, /pipeline-runs, /pipeline-runs/{run_id},
                  /collection-quality, /agent-sessions/{id}, /agent-tool-call-log
    Lớp B:        GET /review-queue, /ops-alerts, /decisions;  POST /decisions
                  POST /agent-sessions/{id}/abort (nút manual abort của Tier 2, mục 9.1)
    Continuity:   POST /exceptions (khai báo ngoại lệ khẩn cấp), GET /exceptions[/{id}[/policy]],
                  GET /config-changes, GET /blocked-connections   (báo cáo mục 3.10.4-3.10.5)
    Gatekeeper:   POST /agent-tool-calls/{tool_name}

Đọc dùng kết nối `mode=ro`; chỉ 4 đường ghi: human_decisions (decisions_store), log
gatekeeper (gatekeeper.log_tool_call), manual_abort_flag (qua `tier2_agent.storage`),
emergency_exceptions (exceptions_store). config_change_log/blocked_connection_log do
`orchestration.cli_continuity` ghi, API chỉ đọc.
Không có route nào ghi dependency_evidence/pipeline_run_log/bảng collector.

PoC: chưa có xác thực người dùng — `decided_by` do client tự khai (xem README).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import _paths  # noqa: F401
from ..db import REASON_CODES, connect, connect_readonly, ensure_schema
from ..wiring import ONLINE_BOUTIQUE_SERVICES
from . import readers
from .decisions_store import insert_decision
from .exceptions import DEFAULT_MAX_TTL, ExceptionValidationError, render_policies, validate_declaration
from .exceptions_store import insert_exception
from .gatekeeper import Gatekeeper, GatekeeperRejection, PRSource, SlidingWindowRateLimiter
from .review import REASONS, DecisionValidationError, validate_decision


class _EmptyPRSource:
    """Mặc định khi chưa cấu hình GitHub — trả rỗng thay vì lỗi (Tier 2 hiểu là "không có PR")."""

    def list_pull_requests(self, service: str) -> list[dict]:
        return []


@dataclass
class ApiSettings:
    db_path: Path
    allowed_services: frozenset = frozenset(ONLINE_BOUTIQUE_SERVICES)
    # Đích nhạy cảm cho HIGH_BLAST_RADIUS (MIGRATION_PLANNER_DESIGN.md mục 1.5: kube-system /
    # EXTERNAL — 2/3 tiêu chí, bỏ RBAC-đọc-Secret). scope=EXTERNAL xét riêng.
    sensitive_namespaces: frozenset = frozenset({"kube-system"})
    rate_limit_per_minute: int = 30
    admin_error_streak: int = 2           # = Tier2Config.max_consecutive_tool_errors
    pr_source: PRSource = field(default_factory=_EmptyPRSource)
    max_exception_ttl: timedelta = DEFAULT_MAX_TTL       # Bảng 3.9: hạn tối đa của ngoại lệ
    clock: Any = None                                    # test truyền hàm trả datetime cố định


class DecisionIn(BaseModel):
    reason_code: str = Field(..., description=f"1 trong {len(REASON_CODES)} reason_code")
    target_ref: dict[str, Any]
    decided_by: str = Field(..., min_length=1)
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


def create_app(settings: ApiSettings) -> FastAPI:
    boot = connect(settings.db_path)
    ensure_schema(boot)  # chỉ DDL — xem db.py
    boot.close()

    gatekeeper = Gatekeeper(settings.pr_source, settings.allowed_services,
                            SlidingWindowRateLimiter(settings.rate_limit_per_minute, 60.0))
    app = FastAPI(title="KLTN Orchestration API", version="1.0",
                  description="Dashboard (đọc) + human review + gatekeeper Tier 2 — ORCHESTRATION_DESIGN.md mục 6")

    def now() -> datetime:
        return settings.clock() if settings.clock else datetime.now(timezone.utc)

    @contextmanager
    def ro():
        conn = connect_readonly(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def rw():
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ Lớp A
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/dependencies")
    def dependencies(run_id: Optional[str] = None, status: Optional[str] = None, history: bool = False):
        with ro() as conn:
            if history:
                if not run_id:
                    raise HTTPException(400, "history=true cần run_id")
                return readers.dependency_history(conn, run_id)
            return readers.list_dependencies(conn, run_id=run_id, status=status)

    @app.get("/dependencies/{evidence_id}")
    def dependency(evidence_id: int):
        with ro() as conn:
            dep = readers.get_dependency(conn, evidence_id)
        if dep is None:
            raise HTTPException(404, "không có dependency_evidence với id này")
        return dep

    @app.get("/pipeline-runs")
    def pipeline_runs(run_id: Optional[str] = None):
        with ro() as conn:
            return readers.list_pipeline_runs(conn, run_id=run_id)

    @app.get("/pipeline-runs/{run_id}")
    def pipeline_run(run_id: str):
        with ro() as conn:
            attempts = readers.list_pipeline_runs(conn, run_id=run_id)
            if not attempts:
                raise HTTPException(404, f"chưa có attempt nào của {run_id}")
            return {"run_id": run_id, "attempts": attempts, "events": readers.pipeline_events(conn, run_id)}

    @app.get("/collection-quality")
    def collection_quality(run_id: Optional[str] = None, attempt_id: Optional[str] = None):
        with ro() as conn:
            return readers.list_collection_quality(conn, run_id=run_id, attempt_id=attempt_id)

    @app.get("/agent-sessions/{session_id}")
    def agent_session(session_id: str):
        with ro() as conn:
            session = readers.get_agent_session(conn, session_id)
        if session is None:
            raise HTTPException(404, "không có phiên Tier 2 này")
        return session

    @app.get("/agent-tool-call-log")
    def tool_call_log(limit: int = Query(200, ge=1, le=2000)):
        with ro() as conn:
            return readers.list_tool_call_log(conn, limit=limit)

    # ------------------------------------------------------------------ Lớp B
    @app.get("/review-queue")
    def review_queue(reason_code: Optional[str] = None, run_id: Optional[str] = None,
                     include_resolved: bool = False):
        if reason_code and reason_code not in REASONS:
            raise HTTPException(400, f"reason_code không hợp lệ: {reason_code}")
        with ro() as conn:
            return readers.review_queue(conn, sensitive_namespaces=settings.sensitive_namespaces,
                                        reason_code=reason_code, run_id=run_id, include_resolved=include_resolved,
                                        now=now())

    @app.get("/ops-alerts")
    def ops_alerts(include_resolved: bool = False):
        with ro() as conn:
            return readers.ops_alerts(conn, error_streak_threshold=settings.admin_error_streak,
                                      include_resolved=include_resolved)

    @app.get("/reason-codes")
    def reason_codes():
        return {code: {"target_kind": s.target_kind, "allowed_actions": sorted(s.actions), "source": s.source,
                       "urgency": s.urgency, "generated_by_review_queue": s.generated_now}
                for code, s in REASONS.items()}

    @app.get("/decisions")
    def decisions(identity_key: Optional[str] = None, run_id: Optional[str] = None,
                  reason_code: Optional[str] = None, resource: Optional[str] = None):
        with ro() as conn:
            return readers.list_decisions(conn, identity_key=identity_key, run_id=run_id,
                                          reason_code=reason_code, resource=resource)

    @app.post("/decisions", status_code=201)
    def post_decision(body: DecisionIn):
        try:
            target, payload = validate_decision(body.reason_code, body.action, body.target_ref, body.payload,
                                                now=now(), max_exception_ttl=settings.max_exception_ttl)
        except DecisionValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        with rw() as conn:
            # quyết định trên ngoại lệ phải trỏ tới ngoại lệ đã khai báo thật
            exc_ids = []
            if REASONS[body.reason_code].target_kind == "exception":
                exc_ids.append(body.target_ref["exception_id"].strip())
            if body.reason_code == "BLOCKED_CONNECTION" and body.action == "APPROVE":
                exc_ids.append(payload["exception_id"].strip())
            for exc_id in exc_ids:
                if conn.execute("SELECT 1 FROM emergency_exceptions WHERE exception_id = ?", (exc_id,)).fetchone() is None:
                    raise HTTPException(422, f"không có ngoại lệ khẩn cấp {exc_id} trong sổ đăng ký")
            return insert_decision(conn, reason_code=body.reason_code, target_ref=body.target_ref, target=target,
                                   decided_by=body.decided_by.strip(), action=body.action, payload=payload)

    @app.post("/agent-sessions/{session_id}/abort")
    def abort_session(session_id: str):
        """Bật `manual_abort_flag` — Brain đọc cờ này ở đầu mỗi vòng (TIER2_DESIGN.md mục 5, 9.1).
        Ghi qua chính `tier2_agent.storage.Storage` (SQL nằm ở code Tier 2)."""
        from tier2_agent.storage import Storage

        storage = Storage(str(settings.db_path))
        try:
            if storage.get_session(session_id) is None:
                raise HTTPException(404, "không có phiên Tier 2 này")
            storage.set_manual_abort(session_id, True)
        finally:
            storage.close()
        return {"tier2_session_id": session_id, "manual_abort_flag": True}

    # ------------------------------------------------------------------ Continuity Monitor
    @app.post("/exceptions", status_code=201)
    def post_exception(body: dict[str, Any] = Body(...)):
        """Khai báo ngoại lệ khẩn cấp (Bảng 3.9). Trả về bản ghi + 2 NetworkPolicy để người quản
        trị tự áp dụng — công cụ không ghi vào cụm."""
        moment = now()
        try:
            rec = validate_declaration(body, now=moment, max_ttl=settings.max_exception_ttl)
        except ExceptionValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        rec["created_at"] = moment.isoformat()
        with rw() as conn:
            row = insert_exception(conn, rec)
        return {**row, "status": "ACTIVE", "policies": render_policies(row)}

    @app.get("/exceptions")
    def exceptions(status: Optional[str] = None):
        with ro() as conn:
            return readers.list_exceptions(conn, now=now(), status=status)

    @app.get("/exceptions/{exception_id}")
    def exception(exception_id: str):
        with ro() as conn:
            exc = readers.get_exception(conn, exception_id, now=now())
        if exc is None:
            raise HTTPException(404, "không có ngoại lệ này")
        return exc

    @app.get("/exceptions/{exception_id}/policy", response_class=PlainTextResponse)
    def exception_policy(exception_id: str):
        import yaml

        with ro() as conn:
            exc = readers.get_exception(conn, exception_id, now=now())
        if exc is None:
            raise HTTPException(404, "không có ngoại lệ này")
        return yaml.safe_dump_all(exc["policies"], sort_keys=False, allow_unicode=True)

    @app.get("/config-changes")
    def config_changes(unattributed_only: bool = False):
        with ro() as conn:
            return readers.list_config_changes(conn, now=now(), unattributed_only=unattributed_only)

    @app.get("/blocked-connections")
    def blocked_connections(flow_key: Optional[str] = None):
        with ro() as conn:
            if flow_key:
                return readers.blocked_flow_events(conn, flow_key)
            return readers.list_blocked_flows(conn)

    # ------------------------------------------------------------------ Gatekeeper
    @app.post("/agent-tool-calls/{tool_name}")
    def agent_tool_call(tool_name: str, request: Request, body: Any = Body(None)):
        caller = request.headers.get("x-tier2-session") or (request.client.host if request.client else None)
        with rw() as conn:
            try:
                return gatekeeper.handle(conn, tool_name, body, caller)
            except GatekeeperRejection as rej:
                return JSONResponse(status_code=rej.http_status, content={"status": rej.status, "reason": rej.reason})

    return app


def settings_from_env() -> ApiSettings:  # pragma: no cover - đọc env của máy chạy thật
    from .gatekeeper import GitHubPRSource

    pr_source: PRSource = _EmptyPRSource()
    repo = os.environ.get("KLTN_PR_REPO", "GoogleCloudPlatform/microservices-demo")
    if os.environ.get("KLTN_PR_SOURCE", "github") == "github":
        pr_source = GitHubPRSource(repo, token=os.environ.get("GITHUB_TOKEN"))
    return ApiSettings(
        db_path=Path(os.environ.get("KLTN_DB", "kltn.sqlite3")),
        rate_limit_per_minute=int(os.environ.get("KLTN_GATEKEEPER_RPM", "30")),
        pr_source=pr_source,
    )
