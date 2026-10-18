"""API Server (ORCHESTRATION_DESIGN.md mục 4-7, DASHBOARD_DESIGN.md mục 2) qua FastAPI TestClient."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from orchestration.api.app import ApiSettings, create_app
from orchestration.batch import BatchOrchestrator
from orchestration.db import connect_readonly, open_db
from orchestration.run_log import PipelineRunLog
from tier1 import storage as tier1_storage
from tier1.schema import DependencyEdgeResult, DependencyStatus, IdentityKey, Scope, ServiceRef, TrafficClass

from .conftest import NS, ScriptedTier2Runner


class FakePRSource:
    def __init__(self, prs=None, error: Exception | None = None):
        self.prs = prs or {}
        self.error = error
        self.calls = []

    def list_pull_requests(self, service):
        self.calls.append(service)
        if self.error:
            raise self.error
        return self.prs.get(service, [])


@pytest.fixture
def make_client(infra_files):
    def _make(**overrides):
        base = dict(db_path=infra_files["db_path"], pr_source=FakePRSource())
        base.update(overrides)
        return TestClient(create_app(ApiSettings(**base)))

    return _make


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def seeded_trial(make_settings, make_deps):
    """trial-01 chạy thật qua Batch Orchestrator: 2 cạnh Tier 2 với kết quả cần người."""
    runner = ScriptedTier2Runner(by_destination={"shippingservice": "CONFLICTING_EVIDENCE",
                                                 None: "INSUFFICIENT_BASIS"})
    outcome = BatchOrchestrator(make_settings(), make_deps(tier2_runner=runner)).run()
    assert outcome.status == "SUCCESS"
    return outcome


def _edge(src, dst, status, *, dest_ns=NS, scope=Scope.IN_CLUSTER, config_finding=None, **kw) -> DependencyEdgeResult:
    return DependencyEdgeResult(
        identity_key=IdentityKey(NS, src, src, dest_ns, dst, "GRPC", 7000),
        destination_service_ref=ServiceRef(dst, dest_ns), scope=scope, traffic_class=TrafficClass.BUSINESS,
        count=100, distinct_days=7, error_ratio=0.0, unstable=False, config_finding=config_finding, score=0.97,
        dependency_status=status, snapshot_id="demo", **kw)


@pytest.fixture
def seed_edges(infra_files):
    """Ghi 1 attempt SUCCESS với các cạnh dựng tay — cho các reason_code mà dữ liệu Online
    Boutique giả không tự sinh (NEEDS_MANUAL_SELECTOR, HIGH_BLAST_RADIUS)."""
    def _seed(run_id: str, results: list) -> str:
        conn = open_db(infra_files["db_path"])
        attempt = f"{run_id}.a1"
        log = PipelineRunLog(conn, run_id, attempt)
        log.record("trial", "RUNNING")
        tier1_storage.insert_results(conn, results, run_id=run_id, attempt_id=attempt)
        log.record("trial", "SUCCESS")
        conn.close()
        return attempt

    return _seed


def _post_decision(client, reason, identity_key, action="APPROVE", run_id=None, payload=None, by="bin"):
    target = {"identity_key": identity_key}
    if run_id:
        target["run_id"] = run_id
    return client.post("/decisions", json={"reason_code": reason, "target_ref": target, "decided_by": by,
                                           "action": action, "payload": payload or {}})


# ---------------------------------------------------------------------------
# Lớp A — đọc
# ---------------------------------------------------------------------------


def test_dependencies_returns_current_state_of_successful_attempt(client, seeded_trial):
    deps = client.get("/dependencies", params={"run_id": "trial-01"}).json()
    assert deps and {d["attempt_id"] for d in deps} == {"trial-01.a1"}
    assert all("decision_id" in d for d in deps)


def test_dependency_detail_includes_config_quote_and_tier2_session(client, seeded_trial):
    deps = client.get("/dependencies", params={"run_id": "trial-01"}).json()
    clean = next(d for d in deps if d["config_finding"] == "FOUND_STRUCTURAL")
    detail = client.get(f"/dependencies/{clean['id']}").json()
    refs = detail["config_evidence"][0]["evidence_refs"]
    assert refs[0]["quote"] == "cartservice:7070" and refs[0]["key"] == "HIDDEN_DEP_TARGET"
    assert detail["is_official_attempt"] is True
    assert client.get("/dependencies/999999").status_code == 404


def test_pipeline_runs_summarise_attempts_and_failed_step(client, make_settings, make_deps, seeded_trial):
    BatchOrchestrator(make_settings(), make_deps(deployment_clock=lambda _ns: None)).run()
    runs = client.get("/pipeline-runs", params={"run_id": "trial-01"}).json()
    by_attempt = {r["attempt_id"]: r for r in runs}
    assert by_attempt["trial-01.a1"]["status"] == "SUCCESS" and by_attempt["trial-01.a1"]["is_official"]
    failed = by_attempt["trial-01.a2"]
    assert failed["status"] == "FAILED" and failed["failed_step"] == "scenario_check"
    assert failed["steps"]["scenario_check"]["status"] == "FAILED"
    detail = client.get("/pipeline-runs/trial-01").json()
    assert detail["events"][0]["status"] == "RUNNING"
    assert client.get("/pipeline-runs/trial-99").status_code == 404


def test_pipeline_runs_show_running_attempt(client, db):
    PipelineRunLog(db, "trial-05", "trial-05.a1").record("trial", "RUNNING")
    PipelineRunLog(db, "trial-05", "trial-05.a1").record("traffic_collector", "RUNNING")
    run = client.get("/pipeline-runs", params={"run_id": "trial-05"}).json()[0]
    assert run["status"] == "RUNNING" and run["finished_at"] is None
    assert run["steps"]["traffic_collector"]["status"] == "RUNNING"


def test_collection_quality_returns_parsed_json(client, seeded_trial):
    rows = client.get("/collection-quality", params={"run_id": "trial-01"}).json()
    config_row = next(r for r in rows if r["module"] == "config_collector")
    assert isinstance(config_row["items_dropped_by_funnel"], dict)


def test_read_connection_cannot_write(infra_files, seeded_trial):
    conn = connect_readonly(infra_files["db_path"])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM dependency_evidence")


# ---------------------------------------------------------------------------
# Lớp B — hàng đợi review (suy ra từ dependency_current_state)
# ---------------------------------------------------------------------------


def test_review_queue_from_real_pipeline_run(client, seeded_trial):
    queue = client.get("/review-queue", params={"run_id": "trial-01"}).json()
    codes = sorted(i["reason_code"] for i in queue)
    assert codes == ["AGENT_INSUFFICIENT_BASIS", "CLEAN_CONFIG_MATCH", "CONFLICTING_EVIDENCE"]
    assert queue[0]["reason_code"] == "CONFLICTING_EVIDENCE"  # urgency cao xếp trước
    clean = next(i for i in queue if i["reason_code"] == "CLEAN_CONFIG_MATCH")
    assert clean["config_evidence"][0]["evidence_refs"][0]["quote"] == "cartservice:7070"
    assert clean["allowed_actions"] == ["APPROVE", "REJECT"]


def test_review_queue_manual_selector_and_high_blast_radius(client, seed_edges):
    seed_edges("demo-01", [
        _edge("frontend", "cartservice", DependencyStatus.NEEDS_MANUAL_SELECTOR, manual_selector_reason="EMPTY_SELECTOR"),
        _edge("checkoutservice", "kube-dns", DependencyStatus.OBSERVED_STRONG, dest_ns="kube-system"),
        _edge("paymentservice", "api.stripe.com", DependencyStatus.OBSERVED_STRONG, scope=Scope.EXTERNAL),
        _edge("frontend", "coredns", DependencyStatus.OBSERVED_STRONG, dest_ns="kube-system", dns_auto_allow=True),
        _edge("frontend", "adservice", DependencyStatus.OBSERVED_STRONG),
    ])
    queue = client.get("/review-queue", params={"run_id": "demo-01"}).json()
    got = sorted((i["reason_code"], i["identity_key"].split("|")[4]) for i in queue)
    # DNS auto-allow (coredns) và cạnh STRONG thường (adservice) KHÔNG vào hàng đợi
    assert got == [("HIGH_BLAST_RADIUS", "api.stripe.com"), ("HIGH_BLAST_RADIUS", "kube-dns"),
                   ("NEEDS_MANUAL_SELECTOR", "cartservice")]
    only = client.get("/review-queue", params={"reason_code": "NEEDS_MANUAL_SELECTOR"}).json()
    assert [i["manual_selector_reason"] for i in only] == ["EMPTY_SELECTOR"]
    assert client.get("/review-queue", params={"reason_code": "NOPE"}).status_code == 400


def test_decision_removes_item_from_queue_and_survives_rerun(client, seeded_trial, make_settings, make_deps):
    """ORCHESTRATION_DESIGN.md mục 3: chạy lại trial (tính lại score) KHÔNG làm mất quyết định người."""
    item = next(i for i in client.get("/review-queue").json() if i["reason_code"] == "CLEAN_CONFIG_MATCH")
    resp = _post_decision(client, "CLEAN_CONFIG_MATCH", item["identity_key"], run_id="trial-01")
    assert resp.status_code == 201

    assert "CLEAN_CONFIG_MATCH" not in {i["reason_code"] for i in client.get("/review-queue").json()}
    resolved = client.get("/review-queue", params={"include_resolved": True}).json()
    assert any(i["resolved_by"] and i["resolved_by"]["decided_by"] == "bin" for i in resolved)

    second = BatchOrchestrator(make_settings(), make_deps()).run()
    assert second.attempt_id == "trial-01.a2"
    deps = client.get("/dependencies", params={"run_id": "trial-01"}).json()
    edge = next(d for d in deps if d["identity_key"] == item["identity_key"])
    assert edge["attempt_id"] == "trial-01.a2"
    assert edge["decision_action"] == "APPROVE" and edge["decision_reason_code"] == "CLEAN_CONFIG_MATCH"
    assert "CLEAN_CONFIG_MATCH" not in {i["reason_code"] for i in client.get("/review-queue").json()}


def test_run_scoped_decision_does_not_leak_to_other_trial(client, seed_edges):
    edge = _edge("frontend", "cartservice", DependencyStatus.NEEDS_MANUAL_SELECTOR)
    seed_edges("demo-01", [edge])
    seed_edges("demo-02", [edge])
    _post_decision(client, "NEEDS_MANUAL_SELECTOR", edge.identity_key.as_string(), action="SUBMIT_SELECTOR",
                   run_id="demo-01", payload={"pod_selector": {"app": "cartservice"}})
    remaining = {i["run_id"] for i in client.get("/review-queue").json()}
    assert remaining == {"demo-02"}


def test_latest_decision_wins_in_current_state(client, seed_edges):
    edge = _edge("frontend", "cartservice", DependencyStatus.NEEDS_MANUAL_SELECTOR)
    seed_edges("demo-01", [edge])
    key = edge.identity_key.as_string()
    _post_decision(client, "NEEDS_MANUAL_SELECTOR", key, action="REJECT")
    _post_decision(client, "NEEDS_MANUAL_SELECTOR", key, action="SUBMIT_SELECTOR",
                   payload={"pod_selector": {"app": "cartservice"}})
    current = client.get("/dependencies", params={"run_id": "demo-01"}).json()[0]
    assert current["decision_action"] == "SUBMIT_SELECTOR"
    assert current["decision_payload"] == {"pod_selector": {"app": "cartservice"}}
    assert len(client.get("/decisions", params={"identity_key": key}).json()) == 2  # lịch sử còn đủ


# ---------------------------------------------------------------------------
# POST /decisions — 11 reason_code, luật action + payload
# ---------------------------------------------------------------------------

KEY = f"{NS}|frontend|frontend|{NS}|cartservice|GRPC|7070"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()


@pytest.mark.parametrize("reason,target,action,payload", [
    ("CLEAN_CONFIG_MATCH", {"identity_key": KEY}, "REJECT", {}),
    ("NEEDS_MANUAL_SELECTOR", {"identity_key": KEY}, "SUBMIT_SELECTOR", {"ip_block": "10.0.0.0/24"}),
    ("HIGH_BLAST_RADIUS", {"identity_key": KEY, "run_id": "trial-01"}, "APPROVE", {}),
    ("CONFLICTING_EVIDENCE", {"identity_key": KEY}, "APPROVE", {"note": "đã đọc trace"}),
    ("AGENT_INSUFFICIENT_BASIS", {"identity_key": KEY}, "REJECT", {}),
    ("TOOL_ADMIN_ATTENTION", {"tier2_session_id": "trial-01.a1#abc"}, "APPROVE", {"note": "đã cấp lại token"}),
    ("POLICY_WOULD_BREAK", {"resource_kind": "NetworkPolicy", "namespace": NS, "name": "deny-all"},
     "OVERRIDE_WITH_JUSTIFICATION", {"justification": "sự cố P1, cần mở tạm cho payment"}),
    ("POLICY_UNSUPPORTED_BY_EVIDENCE", {"resource_kind": "NetworkPolicy", "namespace": NS, "name": "p"}, "APPROVE", {}),
    ("UNATTRIBUTED_CONFIG_CHANGE", {"resource_kind": "Deployment", "namespace": NS, "name": "frontend"},
     "ATTRIBUTE", {"owner": "team-web"}),
    ("EMERGENCY_EXCEPTION_EXPIRED", {"exception_id": "exc_12"}, "EXTEND_EXCEPTION", {"new_expires_at": FUTURE}),
    ("STALE_PERMISSION", {"identity_key": KEY}, "REVOKE", {}),
])
def test_each_reason_code_accepts_its_valid_action(client, reason, target, action, payload):
    resp = client.post("/decisions", json={"reason_code": reason, "target_ref": target, "decided_by": "bin",
                                           "action": action, "payload": payload})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["decision_id"].startswith("dec_") and body["reason_code"] == reason


@pytest.mark.parametrize("reason,target,action,payload,fragment", [
    ("CLEAN_CONFIG_MATCH", {"identity_key": KEY}, "SUBMIT_SELECTOR", {"pod_selector": {"a": "b"}}, "không áp dụng"),
    ("NEEDS_MANUAL_SELECTOR", {"identity_key": KEY}, "SUBMIT_SELECTOR", {}, "pod_selector"),
    ("NEEDS_MANUAL_SELECTOR", {"identity_key": KEY}, "SUBMIT_SELECTOR", {"ip_block": "999.1.1.1/40"}, "CIDR"),
    ("NEEDS_MANUAL_SELECTOR", {"identity_key": KEY}, "SUBMIT_SELECTOR", {"pod_selector": {}}, "pod_selector"),
    ("POLICY_WOULD_BREAK", {"resource_kind": "NetworkPolicy", "namespace": NS, "name": "p"},
     "OVERRIDE_WITH_JUSTIFICATION", {"justification": "ok"}, "quá ngắn"),
    ("POLICY_WOULD_BREAK", {"identity_key": KEY}, "REJECT", {}, "resource_kind"),
    ("EMERGENCY_EXCEPTION_EXPIRED", {"exception_id": "e"}, "EXTEND_EXCEPTION",
     {"new_expires_at": "2020-01-01T00:00:00+00:00"}, "tương lai"),
    ("UNATTRIBUTED_CONFIG_CHANGE", {"resource_kind": "Deployment", "namespace": NS, "name": "x"}, "ATTRIBUTE", {}, "owner"),
    ("TOOL_ADMIN_ATTENTION", {"tier2_session_id": "s"}, "APPROVE", {}, "note"),
    ("STALE_PERMISSION", {"identity_key": "khong-dung-dinh-dang"}, "REVOKE", {}, "7 phần"),
    ("NOT_A_REASON", {"identity_key": KEY}, "APPROVE", {}, "reason_code"),
    ("STALE_PERMISSION", {"identity_key": KEY}, "NUKE", {}, "action"),
])
def test_invalid_decisions_rejected_with_422_and_not_stored(client, reason, target, action, payload, fragment):
    resp = client.post("/decisions", json={"reason_code": reason, "target_ref": target, "decided_by": "bin",
                                           "action": action, "payload": payload})
    assert resp.status_code == 422
    assert fragment in resp.text
    assert client.get("/decisions").json() == []


def test_decided_by_required(client):
    resp = client.post("/decisions", json={"reason_code": "STALE_PERMISSION", "target_ref": {"identity_key": KEY},
                                           "decided_by": "", "action": "REVOKE"})
    assert resp.status_code == 422


def test_reason_codes_catalogue(client):
    cat = client.get("/reason-codes").json()
    assert len(cat) == 11
    assert cat["EMERGENCY_EXCEPTION_EXPIRED"]["allowed_actions"] == ["EXTEND_EXCEPTION", "MAKE_PERMANENT", "REVOKE"]
    assert cat["STALE_PERMISSION"]["generated_by_review_queue"] is False


# ---------------------------------------------------------------------------
# Cảnh báo vận hành TOOL_ADMIN_ATTENTION + manual abort
# ---------------------------------------------------------------------------


def _seed_tier2_session(db_path, session_id, statuses):
    from tier2_agent.models import EvidenceRow, SessionState
    from tier2_agent.storage import Storage

    st = Storage(str(db_path))
    st.create_session(SessionState(run_id=session_id, edge_identity_key=KEY, session_started_at=1.0))
    for i, status in enumerate(statuses):
        st.append_trace_row(EvidenceRow(evidence_id=f"{session_id}-E{i}", run_id=session_id, iteration=i + 1,
                                        hypothesis_id="H1", tool_name="read_pr_descriptions",
                                        tool_input={"service": "frontend"}, tool_output=None, stance=None,
                                        stale=False, status=status,
                                        error_message="gatekeeper 502" if status == "ERROR" else None))
    st.close()


def test_ops_alert_when_tool_fails_twice_in_a_row(client, infra_files):
    _seed_tier2_session(infra_files["db_path"], "s-bad", ["ERROR", "ERROR"])
    _seed_tier2_session(infra_files["db_path"], "s-ok", ["ERROR", "OK", "ERROR"])
    alerts = client.get("/ops-alerts").json()
    assert [(a["tier2_session_id"], a["consecutive_errors"]) for a in alerts] == [("s-bad", 2)]
    assert alerts[0]["last_error"] == "gatekeeper 502"
    assert client.get("/review-queue").json() == []  # tách khỏi hàng đợi nghiệp vụ

    resp = client.post("/decisions", json={"reason_code": "TOOL_ADMIN_ATTENTION",
                                           "target_ref": {"tier2_session_id": "s-bad"}, "decided_by": "bin",
                                           "action": "APPROVE", "payload": {"note": "đã sửa token GitHub"}})
    assert resp.status_code == 201
    assert client.get("/ops-alerts").json() == []


def test_manual_abort_sets_flag(client, infra_files):
    _seed_tier2_session(infra_files["db_path"], "s-1", [])
    assert client.post("/agent-sessions/s-1/abort").json()["manual_abort_flag"] is True
    session = client.get("/agent-sessions/s-1").json()
    assert session["manual_abort_flag"] == 1
    assert client.post("/agent-sessions/nope/abort").status_code == 404
    assert client.get("/agent-sessions/nope").status_code == 404


# ---------------------------------------------------------------------------
# Gatekeeper /agent-tool-calls/* (ORCHESTRATION_DESIGN.md mục 7)
# ---------------------------------------------------------------------------


def _log(db_path):
    conn = open_db(db_path)
    rows = [dict(r) for r in conn.execute("SELECT * FROM agent_tool_call_log ORDER BY id")]
    conn.close()
    return rows


def test_gatekeeper_returns_prs_in_tier2_contract_and_logs(make_client, infra_files):
    source = FakePRSource({"frontend": [{"number": 42, "title": "t", "body": "b" * 9000,
                                         "changed_files": ["src/frontend/main.go"]}]})
    client = make_client(pr_source=source)
    resp = client.post("/agent-tool-calls/read_pr_descriptions", json={"service": "frontend"},
                       headers={"X-Tier2-Session": "trial-01.a1#abc"})
    assert resp.status_code == 200
    pr = resp.json()["pull_requests"][0]
    assert pr["number"] == 42 and len(pr["body"]) == 4000
    log = _log(infra_files["db_path"])
    assert log[-1]["status"] == "OK" and log[-1]["caller"] == "trial-01.a1#abc" and log[-1]["result_count"] == 1


def test_gatekeeper_works_with_real_tier2_http_client(make_client):
    """Hợp đồng HTTP khớp đúng `tier2_agent.tools.pr_descriptions.make_http_gatekeeper_fetch`."""
    from tier2_agent.tools.pr_descriptions import make_http_gatekeeper_fetch

    client = make_client(pr_source=FakePRSource({"cartservice": [{"number": 7, "title": "x", "body": "y",
                                                                  "changed_files": []}]}))
    fetch = make_http_gatekeeper_fetch("http://testserver", session=client)
    assert fetch("cartservice")[0]["number"] == 7


@pytest.mark.parametrize("tool,body,code", [
    ("read_source_code", {"service": "frontend"}, 403),                 # không trong allowlist
    ("read_pr_descriptions", {"service": "evil-service"}, 403),         # ngoài whitelist
    ("read_pr_descriptions", {"service": "frontend", "token": "x"}, 400),  # field lạ
    ("read_pr_descriptions", ["frontend"], 400),
])
def test_gatekeeper_rejections_are_logged(client, infra_files, tool, body, code):
    resp = client.post(f"/agent-tool-calls/{tool}", json=body)
    assert resp.status_code == code
    assert _log(infra_files["db_path"])[-1]["status"] == "REJECTED"


def test_gatekeeper_rate_limit(make_client, infra_files):
    client = make_client(rate_limit_per_minute=2)
    codes = [client.post("/agent-tool-calls/read_pr_descriptions", json={"service": "frontend"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
    assert [r["status"] for r in _log(infra_files["db_path"])] == ["OK", "OK", "RATE_LIMITED"]


def test_gatekeeper_upstream_error_is_502_and_logged(make_client, infra_files):
    client = make_client(pr_source=FakePRSource(error=TimeoutError("github chậm")))
    resp = client.post("/agent-tool-calls/read_pr_descriptions", json={"service": "frontend"})
    assert resp.status_code == 502
    assert "TimeoutError" in _log(infra_files["db_path"])[-1]["reason"]
    assert client.get("/agent-tool-call-log").json()[0]["status"] == "ERROR"


def test_sliding_window_rate_limiter_recovers_after_window():
    from orchestration.api.gatekeeper import SlidingWindowRateLimiter

    t = [0.0]
    limiter = SlidingWindowRateLimiter(1, 60.0, clock=lambda: t[0])
    assert limiter.allow("x") and not limiter.allow("x")
    t[0] = 61.0
    assert limiter.allow("x")


def test_dependencies_history_requires_run_id(client, seeded_trial):
    assert client.get("/dependencies", params={"history": True}).status_code == 400
    rows = client.get("/dependencies", params={"history": True, "run_id": "trial-01"}).json()
    assert rows and json.dumps(rows)
