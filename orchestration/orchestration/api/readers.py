"""Truy vấn CHỈ ĐỌC cho Lớp A (dashboard) và phần đọc của Lớp B (hàng đợi review).

Mọi hàm nhận 1 kết nối `db.connect_readonly()` — SQLite từ chối ghi trên kết nối đó. Không
có câu INSERT/UPDATE/DELETE nào trong file này (test_write_boundaries.py kiểm tra).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .exceptions import covers_change, effective_state, parse_time, render_policies
from .review import REASONS, URGENCY_ORDER, edge_review_reasons, max_consecutive_errors

_JSON_COLUMNS = {"detail", "payload", "decision_payload", "target_ref", "flags", "evidence_refs",
                 "items_dropped_by_funnel", "scrape_gap_targets", "system_notices", "stance_updates",
                 "new_hypotheses", "tool_input", "source_pod_selector", "destination_pod_selector"}


def _row(r: sqlite3.Row) -> dict:
    out = dict(r)
    for k in _JSON_COLUMNS & set(out):
        if isinstance(out[k], str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    return out


def _rows(cur) -> list[dict]:
    return [_row(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Lớp A — dependency, pipeline runs, chất lượng thu thập
# ---------------------------------------------------------------------------


def list_dependencies(conn, run_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM dependency_current_state WHERE 1=1"
    params: list = []
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    if status:
        sql += " AND (dependency_status = ? OR tier2_status = ?)"
        params += [status, status]
    sql += " ORDER BY run_id, id"
    return _rows(conn.execute(sql, params))


def dependency_history(conn, run_id: str) -> list[dict]:
    """Mọi dòng của mọi attempt (kể cả attempt FAILED) — để truy vết, không phải trạng thái chính thức."""
    return _rows(conn.execute("SELECT * FROM dependency_evidence WHERE run_id = ? ORDER BY id", (run_id,)))


def get_dependency(conn, evidence_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM dependency_evidence WHERE id = ?", (evidence_id,)).fetchone()
    if row is None:
        return None
    dep = _row(row)
    dep["config_evidence"] = config_evidence_for(conn, dep)
    dep["decisions"] = list_decisions(conn, identity_key=dep.get("identity_key"))
    dep["tier2_session"] = (
        get_agent_session(conn, dep["tier2_session_id"]) if dep.get("tier2_session_id") else None
    )
    official = conn.execute("SELECT 1 FROM latest_successful_attempt WHERE attempt_id = ?",
                            (dep.get("attempt_id"),)).fetchone()
    dep["is_official_attempt"] = official is not None
    return dep


def config_evidence_for(conn, dep: dict) -> list[dict]:
    """Truy ngược 1 cạnh về đúng dòng config nguyên văn đã sinh ra nó (DASHBOARD_DESIGN.md
    mục 4): `config_evidence.run_id` = attempt_id (Batch Orchestrator), khớp theo cặp
    nguồn/đích; `evidence_refs` chứa `quote`/`source_kind`/`key`."""
    if not dep.get("attempt_id"):
        return []
    return _rows(conn.execute(
        "SELECT * FROM config_evidence WHERE run_id = ? AND source_namespace = ? AND source_canonical_service = ? "
        "AND destination_namespace IS ? AND destination_canonical_service IS ? ORDER BY id",
        (dep["attempt_id"], dep["source_namespace"], dep["source_canonical_service"],
         dep["destination_namespace"], dep["destination_canonical_service"]),
    ))


def list_pipeline_runs(conn, run_id: Optional[str] = None) -> list[dict]:
    """1 phần tử / attempt: trạng thái trial + trạng thái mới nhất từng bước + bước lỗi."""
    params: list = []
    where = ""
    if run_id:
        where = "WHERE run_id = ?"
        params.append(run_id)
    steps = _rows(conn.execute(f"SELECT * FROM pipeline_step_status {where} ORDER BY id", params))
    started = {r["attempt_id"]: r["started_at"] for r in conn.execute(
        f"SELECT attempt_id, MIN(timestamp) AS started_at FROM pipeline_run_log {where} GROUP BY attempt_id", params)}
    official = {r["attempt_id"] for r in conn.execute("SELECT attempt_id FROM latest_successful_attempt")}
    attempts: dict[str, dict] = {}
    for s in steps:
        a = attempts.setdefault(s["attempt_id"], {
            "run_id": s["run_id"], "attempt_id": s["attempt_id"], "status": None, "failed_step": None,
            "error_message": None, "started_at": started.get(s["attempt_id"]), "finished_at": None,
            "is_official": s["attempt_id"] in official, "steps": {},
        })
        if s["step"] == "trial":
            a["status"] = s["status"]
            a["finished_at"] = s["timestamp"] if s["status"] != "RUNNING" else None
            if s["status"] == "FAILED":
                a["error_message"] = s["error_message"]
                a["failed_step"] = (s.get("detail") or {}).get("failed_step")
        else:
            a["steps"][s["step"]] = {"status": s["status"], "timestamp": s["timestamp"],
                                     "error_message": s["error_message"], "detail": s.get("detail")}
    return sorted(attempts.values(), key=lambda a: (a["run_id"], a["started_at"] or ""))


def pipeline_events(conn, run_id: str) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM pipeline_run_log WHERE run_id = ? ORDER BY id", (run_id,)))


def list_collection_quality(conn, run_id: Optional[str] = None, attempt_id: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM collection_quality_log WHERE 1=1"
    params: list = []
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    if attempt_id:
        sql += " AND attempt_id = ?"
        params.append(attempt_id)
    return _rows(conn.execute(sql + " ORDER BY id", params))


# ---------------------------------------------------------------------------
# Tier 2 — phiên, vòng suy luận, trace
# ---------------------------------------------------------------------------


def get_agent_session(conn, session_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM agent_sessions WHERE run_id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    session = _row(row)
    session["rounds"] = _rows(conn.execute(
        "SELECT * FROM agent_rounds WHERE run_id = ? ORDER BY iteration", (session_id,)))
    session["trace"] = _rows(conn.execute(
        "SELECT * FROM trace_log WHERE run_id = ? ORDER BY iteration, rowid", (session_id,)))
    return session


# ---------------------------------------------------------------------------
# Lớp B — quyết định, hàng đợi review, cảnh báo vận hành
# ---------------------------------------------------------------------------


def list_decisions(conn, identity_key: Optional[str] = None, run_id: Optional[str] = None,
                   reason_code: Optional[str] = None, resource: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM human_decisions WHERE 1=1"
    params: list = []
    if identity_key:
        sql += " AND target_identity_key = ?"
        params.append(identity_key)
    if run_id:
        sql += " AND (target_run_id IS NULL OR target_run_id = ?)"
        params.append(run_id)
    if reason_code:
        sql += " AND reason_code = ?"
        params.append(reason_code)
    if resource:
        sql += " AND target_resource = ?"
        params.append(resource)
    return _rows(conn.execute(sql + " ORDER BY decided_at, rowid", params))


def _resolving_decision(conn, reason_code: str, identity_key: str, run_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM human_decisions WHERE reason_code = ? AND target_identity_key = ? "
        "AND (target_run_id IS NULL OR target_run_id = ?) ORDER BY decided_at DESC, rowid DESC LIMIT 1",
        (reason_code, identity_key, run_id),
    ).fetchone()
    return _row(row) if row else None


def review_queue(conn, *, sensitive_namespaces: frozenset, reason_code: Optional[str] = None,
                 run_id: Optional[str] = None, include_resolved: bool = False,
                 now: Optional[datetime] = None) -> list[dict]:
    """Hàng đợi `PENDING_HUMAN_REVIEW` — suy ra lúc query từ `dependency_current_state`
    (không có bảng hàng đợi riêng phải đồng bộ). Mỗi cạnh xử lý độc lập; 1 cạnh chờ người
    KHÔNG chặn cạnh khác (ORCHESTRATION_DESIGN.md mục 4). Đã có quyết định cùng reason_code
    cho cùng identity_key (+ cùng trial hoặc áp mọi trial) -> coi như đã xử lý."""
    items = []
    for dep in list_dependencies(conn, run_id=run_id):
        for code in edge_review_reasons(dep, sensitive_namespaces):
            if reason_code and code != reason_code:
                continue
            decision = _resolving_decision(conn, code, dep["identity_key"], dep["run_id"])
            if decision and not include_resolved:
                continue
            spec = REASONS[code]
            item = {
                "queue_item_id": f"{code}:{dep['run_id']}:{dep['identity_key']}",
                "reason_code": code,
                "urgency": spec.urgency,
                "source": spec.source,
                "allowed_actions": sorted(spec.actions),
                "target_ref": {"identity_key": dep["identity_key"], "run_id": dep["run_id"]},
                "run_id": dep["run_id"],
                "attempt_id": dep["attempt_id"],
                "evidence_id": dep["id"],
                "identity_key": dep["identity_key"],
                "dependency_status": dep["dependency_status"],
                "tier2_status": dep.get("tier2_status"),
                "tier2_session_id": dep.get("tier2_session_id"),
                "config_finding": dep.get("config_finding"),
                "score": dep.get("score"),
                "manual_selector_reason": dep.get("manual_selector_reason"),
                "resolved_by": decision,
            }
            if code == "CLEAN_CONFIG_MATCH":
                # Màn hình hiện kèm đoạn config nguyên văn + nguồn (DASHBOARD_DESIGN.md mục 2)
                item["config_evidence"] = [
                    {"evidence_refs": c.get("evidence_refs"), "raw_host": c.get("raw_host"),
                     "config_finding": c.get("config_finding")}
                    for c in config_evidence_for(conn, dep)
                ]
            items.append(item)
    if run_id is None:   # việc của Continuity Monitor không thuộc trial nào
        items += [i for i in continuity_review_items(conn, now=now or datetime.now(timezone.utc),
                                                     include_resolved=include_resolved)
                  if not reason_code or i["reason_code"] == reason_code]
    items.sort(key=lambda i: (URGENCY_ORDER[i["urgency"]], i.get("run_id") or "", i.get("evidence_id") or 0,
                              i["queue_item_id"]))
    return items


def ops_alerts(conn, *, error_streak_threshold: int, include_resolved: bool = False) -> list[dict]:
    """`TOOL_ADMIN_ATTENTION` — mục riêng "Cảnh báo vận hành", tách khỏi hàng đợi nghiệp vụ
    (DASHBOARD_DESIGN.md mục 2). Phát hiện từ `trace_log`: 1 tool lỗi liên tiếp >= ngưỡng
    (`Tier2Config.max_consecutive_tool_errors`, mặc định 2) trong cùng 1 phiên."""
    alerts = []
    sessions = _rows(conn.execute("SELECT * FROM agent_sessions ORDER BY session_started_at"))
    for s in sessions:
        trace = _rows(conn.execute(
            "SELECT tool_name, status, error_message FROM trace_log WHERE run_id = ? ORDER BY iteration, rowid",
            (s["run_id"],)))
        for tool, (streak, last_error) in max_consecutive_errors(trace).items():
            if streak < error_streak_threshold:
                continue
            resource = f"agent_session/{s['run_id']}"
            decision = conn.execute(
                "SELECT * FROM human_decisions WHERE reason_code = 'TOOL_ADMIN_ATTENTION' AND target_resource = ? "
                "ORDER BY decided_at DESC, rowid DESC LIMIT 1", (resource,)).fetchone()
            if decision and not include_resolved:
                continue
            alerts.append({
                "reason_code": "TOOL_ADMIN_ATTENTION",
                "tier2_session_id": s["run_id"],
                "edge_identity_key": s["edge_identity_key"],
                "tool_name": tool,
                "consecutive_errors": streak,
                "last_error": last_error,
                "allowed_actions": sorted(REASONS["TOOL_ADMIN_ATTENTION"].actions),
                "target_ref": {"tier2_session_id": s["run_id"]},
                "resolved_by": _row(decision) if decision else None,
            })
    return alerts


def list_tool_call_log(conn, limit: int = 200) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM agent_tool_call_log ORDER BY id DESC LIMIT ?", (limit,)))


# ---------------------------------------------------------------------------
# Continuity Monitor — ngoại lệ khẩn cấp, thay đổi chính sách, kết nối bị chặn (19/11)
# Mọi trạng thái SUY RA lúc đọc; không bảng nào bị UPDATE.
# ---------------------------------------------------------------------------


def _resource_decisions(conn, resource: str, reason_code: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM human_decisions WHERE target_resource = ?"
    params: list = [resource]
    if reason_code:
        sql += " AND reason_code = ?"
        params.append(reason_code)
    return _rows(conn.execute(sql + " ORDER BY decided_at, rowid", params))


def _with_state(conn, exc: dict, now: datetime) -> dict:
    decisions = _resource_decisions(conn, f"emergency_exception/{exc['exception_id']}")
    state = effective_state(exc, decisions, now)
    out = dict(exc)
    out.update(status=state.status, effective_expires_at=state.effective_expires_at, decisions=decisions)
    return out


def list_exceptions(conn, *, now: datetime, status: Optional[str] = None) -> list[dict]:
    rows = _rows(conn.execute("SELECT * FROM emergency_exceptions ORDER BY created_at, rowid"))
    out = [_with_state(conn, r, now) for r in rows]
    return [r for r in out if not status or r["status"] == status]


def get_exception(conn, exception_id: str, *, now: datetime) -> Optional[dict]:
    row = conn.execute("SELECT * FROM emergency_exceptions WHERE exception_id = ?", (exception_id,)).fetchone()
    if row is None:
        return None
    exc = _with_state(conn, _row(row), now)
    exc["policies"] = render_policies(exc)
    exc["traffic_support"] = traffic_supported(conn, exc)
    return exc


def traffic_supported(conn, exc: dict) -> bool:
    """MIGRATION_PLANNER_DESIGN.md mục 2.5b bước 3: ngoại lệ còn lưu lượng thật (Tầng 1 kết luận
    OBSERVED_STRONG cho đúng cạnh) -> có vẻ đang dùng thật, ưu tiên thấp; không còn -> cao."""
    row = conn.execute(
        "SELECT 1 FROM dependency_current_state WHERE source_namespace = ? AND source_canonical_service = ? "
        "AND destination_namespace = ? AND destination_canonical_service = ? AND (port IS NULL OR port = ?) "
        "AND dependency_status = 'OBSERVED_STRONG' LIMIT 1",
        (exc["source_namespace"], exc["source_service"], exc["destination_namespace"],
         exc["destination_service"], exc["port"]),
    ).fetchone()
    return row is not None


def list_config_changes(conn, *, now: datetime, unattributed_only: bool = False) -> list[dict]:
    """Mỗi thay đổi kèm `attributed_to`: 'gitops', 'exception:<id>', hoặc None (chưa quy được)."""
    exceptions = list_exceptions(conn, now=now)
    out = []
    for ch in _rows(conn.execute("SELECT * FROM config_change_log ORDER BY changed_at, rowid")):
        attributed = None
        if ch["source"] == "GITOPS":
            attributed = "gitops"
        else:
            for exc in exceptions:
                state = effective_state(exc, exc["decisions"], now)
                if covers_change(exc, state, ch):
                    attributed = f"exception:{exc['exception_id']}"
                    break
        ch["attributed_to"] = attributed
        if not unattributed_only or attributed is None:
            out.append(ch)
    return out


def list_blocked_flows(conn) -> list[dict]:
    """Gộp các lần chặn theo `flow_key` — 1 kết nối = 1 việc, kèm số lần và lần đầu/cuối."""
    return _rows(conn.execute(
        "SELECT flow_key, COUNT(*) AS block_count, MIN(observed_at) AS first_seen, MAX(observed_at) AS last_seen, "
        "MAX(source_namespace) AS source_namespace, MAX(source_workload) AS source_workload, "
        "MAX(source_service_account) AS source_service_account, MAX(destination_namespace) AS destination_namespace, "
        "MAX(destination_workload) AS destination_workload, MAX(protocol) AS protocol, MAX(port) AS port, "
        "GROUP_CONCAT(DISTINCT reporter) AS reporters "
        "FROM blocked_connection_log GROUP BY flow_key ORDER BY last_seen DESC"))


def blocked_flow_events(conn, flow_key: str) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM blocked_connection_log WHERE flow_key = ? ORDER BY observed_at, id",
                              (flow_key,)))


def _after(decision: Optional[dict], moment: str) -> bool:
    if decision is None:
        return False
    try:
        return parse_time(decision["decided_at"]) >= parse_time(moment)
    except ValueError:
        return True


def continuity_review_items(conn, *, now: datetime, include_resolved: bool = False) -> list[dict]:
    items: list[dict] = []

    # 1. Ngoại lệ quá hạn (mục 2.2 + 2.5b) — nằm trong hàng đợi tới khi người quyết
    for exc in list_exceptions(conn, now=now, status="EXPIRED"):
        support = traffic_supported(conn, exc)
        items.append({
            "queue_item_id": f"EMERGENCY_EXCEPTION_EXPIRED:{exc['exception_id']}:{exc['effective_expires_at']}",
            "reason_code": "EMERGENCY_EXCEPTION_EXPIRED",
            "urgency": "low" if support else "high",
            "source": REASONS["EMERGENCY_EXCEPTION_EXPIRED"].source,
            "allowed_actions": sorted(REASONS["EMERGENCY_EXCEPTION_EXPIRED"].actions),
            "target_ref": {"exception_id": exc["exception_id"]},
            "exception": {k: exc[k] for k in ("requested_by", "reason", "ticket", "source_namespace", "source_service",
                                              "destination_namespace", "destination_service", "protocol", "port",
                                              "resource_name", "created_at", "effective_expires_at")},
            "traffic_support": support,
            "resolved_by": None,
        })

    # 2. Thay đổi chính sách không quy được trách nhiệm (mục 2.5) — 1 việc / resource
    latest: dict[str, dict] = {}
    for ch in list_config_changes(conn, now=now, unattributed_only=True):
        latest[f"{ch['resource_kind']}/{ch['resource_namespace']}/{ch['resource_name']}"] = ch
    for resource, ch in latest.items():
        decisions = _resource_decisions(conn, resource, "UNATTRIBUTED_CONFIG_CHANGE")
        decision = decisions[-1] if decisions else None
        resolved = _after(decision, ch["changed_at"])
        if resolved and not include_resolved:
            continue
        items.append({
            "queue_item_id": f"UNATTRIBUTED_CONFIG_CHANGE:{resource}:{ch['audit_id']}",
            "reason_code": "UNATTRIBUTED_CONFIG_CHANGE",
            "urgency": REASONS["UNATTRIBUTED_CONFIG_CHANGE"].urgency,
            "source": REASONS["UNATTRIBUTED_CONFIG_CHANGE"].source,
            "allowed_actions": sorted(REASONS["UNATTRIBUTED_CONFIG_CHANGE"].actions),
            "target_ref": {"resource_kind": ch["resource_kind"], "namespace": ch["resource_namespace"],
                           "name": ch["resource_name"]},
            "change": {k: ch[k] for k in ("change_id", "change_type", "source", "actor", "changed_at", "audit_id")},
            "resolved_by": decision if resolved else None,
        })

    # 3. Kết nối bị chặn chưa ai xử lý (báo cáo 3.10.4) — chặn lại sau quyết định thì mở lại
    for flow in list_blocked_flows(conn):
        resource = f"blocked_flow/{flow['flow_key']}"
        decisions = _resource_decisions(conn, resource, "BLOCKED_CONNECTION")
        decision = decisions[-1] if decisions else None
        resolved = _after(decision, flow["last_seen"])
        if resolved and not include_resolved:
            continue
        items.append({
            "queue_item_id": f"BLOCKED_CONNECTION:{flow['flow_key']}",
            "reason_code": "BLOCKED_CONNECTION",
            "urgency": REASONS["BLOCKED_CONNECTION"].urgency,
            "source": REASONS["BLOCKED_CONNECTION"].source,
            "allowed_actions": sorted(REASONS["BLOCKED_CONNECTION"].actions),
            "target_ref": {"flow_key": flow["flow_key"]},
            "blocked": flow,
            "resolved_by": decision if resolved else None,
        })
    return items
