"""Continuity Monitor (báo cáo 3.10.4-3.10.5; MIGRATION_PLANNER_DESIGN.md mục 2):
sổ ngoại lệ khẩn cấp, Audit Log -> thay đổi chưa quy trách nhiệm, log kết nối bị chặn."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from orchestration import cli_continuity
from orchestration.api.app import ApiSettings, create_app
from orchestration.continuity.audit import parse_audit_event, parse_audit_log
from orchestration.continuity.blocked import (build_ztunnel_denied_query, collect_ztunnel_denied,
                                              parse_blocked_log)
from orchestration.continuity.store import insert_blocked_connections, insert_config_changes
from orchestration.db import ORCHESTRATION_SCHEMA, open_db
from tier1.log_collector import FakeLogsClient

from .conftest import NS

T0 = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(infra_files, clock):
    return TestClient(create_app(ApiSettings(db_path=infra_files["db_path"], clock=clock)))


@pytest.fixture
def db_path(infra_files, client):  # client tạo schema trước
    return infra_files["db_path"]


def declaration(**over) -> dict:
    body = {"requested_by": "bin", "reason": "sự cố thanh toán", "ticket": "INC-42",
            "source_namespace": NS, "source_service": "checkoutservice", "destination_namespace": NS,
            "destination_service": "paymentservice", "protocol": "GRPC", "port": 50051,
            "expires_at": (T0 + timedelta(days=2)).isoformat()}
    body.update(over)
    return body


def audit_event(audit_id="a1", verb="create", name="zt-exc-x", ns=NS, user="kubernetes-admin",
                at=T0 + timedelta(minutes=5), labels=None, resource="networkpolicies", stage="ResponseComplete",
                code=201):
    ev = {"kind": "Event", "apiVersion": "audit.k8s.io/v1", "auditID": audit_id, "stage": stage, "verb": verb,
          "user": {"username": user},
          "objectRef": {"resource": resource, "namespace": ns, "name": name, "apiGroup": "networking.k8s.io"},
          "responseStatus": {"code": code}, "stageTimestamp": at.isoformat().replace("+00:00", "Z")}
    if labels is not None:
        ev["requestObject"] = {"metadata": {"name": name, "namespace": ns, "labels": labels}}
    return ev


def ingest_audit(db_path, *events):
    conn = open_db(db_path)
    try:
        return insert_config_changes(conn, parse_audit_log(json.dumps(e) for e in events))
    finally:
        conn.close()


def ingest_blocked(db_path, lines, fmt):
    conn = open_db(db_path)
    try:
        return insert_blocked_connections(conn, parse_blocked_log(lines, fmt))
    finally:
        conn.close()


def queue(client, code):
    return [i for i in client.get("/review-queue").json() if i["reason_code"] == code]


def decide(client, code, target, action, payload):
    return client.post("/decisions", json={"reason_code": code, "target_ref": target, "decided_by": "bin",
                                           "action": action, "payload": payload})


# ---------------------------------------------------------------------------
# Sổ đăng ký ngoại lệ (Bảng 3.9)
# ---------------------------------------------------------------------------


def test_declare_exception_returns_record_and_two_policies(client):
    resp = client.post("/exceptions", json=declaration())
    assert resp.status_code == 201, resp.text
    exc = resp.json()
    assert exc["exception_id"].startswith("exc_") and exc["status"] == "ACTIVE"
    assert exc["resource_name"] == "zt-exc-" + exc["exception_id"][4:]
    ingress, egress = exc["policies"]
    assert ingress["metadata"]["namespace"] == NS and ingress["spec"]["policyTypes"] == ["Ingress"]
    assert ingress["metadata"]["labels"]["kltn.zt/exception-id"] == exc["exception_id"]
    assert ingress["metadata"]["annotations"]["kltn.zt/ticket"] == "INC-42"
    assert ingress["spec"]["podSelector"]["matchLabels"] == {"app": "paymentservice"}
    assert ingress["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 50051}]  # GRPC -> TCP
    assert egress["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"] == {"app": "paymentservice"}
    yml = client.get(f"/exceptions/{exc['exception_id']}/policy").text
    assert [d["kind"] for d in yaml.safe_load_all(yml)] == ["NetworkPolicy", "NetworkPolicy"]


@pytest.mark.parametrize("over,fragment", [
    ({"ticket": ""}, "ticket"),
    ({"reason": None}, "reason"),
    ({"expires_at": None}, "vô thời hạn"),
    ({"expires_at": (T0 - timedelta(hours=1)).isoformat()}, "tương lai"),
    ({"expires_at": (T0 + timedelta(days=8)).isoformat()}, "tối đa 7 ngày"),
    ({"port": 0}, "port"),
    ({"port": "80"}, "port"),
    ({"protocol": "ICMP"}, "protocol"),
    ({"source_namespace": "Bad_NS"}, "namespace"),
    ({"destination_pod_selector": {}}, "destination_pod_selector"),
])
def test_declaration_requires_every_field_of_table_3_9(client, over, fragment):
    resp = client.post("/exceptions", json=declaration(**over))
    assert resp.status_code == 422 and fragment in resp.text
    assert client.get("/exceptions").json() == []


def test_exception_table_is_append_only(client, db_path):
    client.post("/exceptions", json=declaration())
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE emergency_exceptions SET expires_at = '2099-01-01'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM emergency_exceptions")


def test_expired_exception_stays_in_queue_until_admin_decides(client, clock):
    exc_id = client.post("/exceptions", json=declaration()).json()["exception_id"]
    assert queue(client, "EMERGENCY_EXCEPTION_EXPIRED") == []

    clock.now = T0 + timedelta(days=3)                       # quá hạn, không ai xử lý
    items = queue(client, "EMERGENCY_EXCEPTION_EXPIRED")
    assert [i["target_ref"] for i in items] == [{"exception_id": exc_id}]
    assert items[0]["urgency"] == "high" and items[0]["traffic_support"] is False   # không có traffic hỗ trợ
    assert client.get(f"/exceptions/{exc_id}").json()["status"] == "EXPIRED"
    clock.now += timedelta(days=5)                           # vẫn còn đó, không tự gỡ, không biến mất
    assert len(queue(client, "EMERGENCY_EXCEPTION_EXPIRED")) == 1

    new_exp = (clock.now + timedelta(days=1)).isoformat()
    r = decide(client, "EMERGENCY_EXCEPTION_EXPIRED", {"exception_id": exc_id}, "EXTEND_EXCEPTION",
               {"new_expires_at": new_exp, "reason": "đối tác chưa sửa xong"})
    assert r.status_code == 201, r.text
    assert queue(client, "EMERGENCY_EXCEPTION_EXPIRED") == []
    assert client.get(f"/exceptions/{exc_id}").json()["status"] == "ACTIVE"

    clock.now += timedelta(days=2)                           # hết hạn lần 2 -> lại nổi lên
    assert len(queue(client, "EMERGENCY_EXCEPTION_EXPIRED")) == 1
    decide(client, "EMERGENCY_EXCEPTION_EXPIRED", {"exception_id": exc_id}, "REVOKE", {})
    assert queue(client, "EMERGENCY_EXCEPTION_EXPIRED") == []
    detail = client.get(f"/exceptions/{exc_id}").json()
    assert detail["status"] == "REVOKED" and [d["action"] for d in detail["decisions"]] == ["EXTEND_EXCEPTION", "REVOKE"]


def test_make_permanent_needs_gitops_ref_and_ends_exception(client, clock):
    exc_id = client.post("/exceptions", json=declaration()).json()["exception_id"]
    assert decide(client, "EMERGENCY_EXCEPTION_EXPIRED", {"exception_id": exc_id}, "MAKE_PERMANENT",
                  {}).status_code == 422
    assert decide(client, "EMERGENCY_EXCEPTION_EXPIRED", {"exception_id": exc_id}, "MAKE_PERMANENT",
                  {"gitops_ref": "PR #57"}).status_code == 201
    clock.now = T0 + timedelta(days=30)
    assert client.get(f"/exceptions/{exc_id}").json()["status"] == "MADE_PERMANENT"
    assert queue(client, "EMERGENCY_EXCEPTION_EXPIRED") == []


def test_extension_capped_at_max_ttl(client):
    exc_id = client.post("/exceptions", json=declaration()).json()["exception_id"]
    r = decide(client, "EMERGENCY_EXCEPTION_EXPIRED", {"exception_id": exc_id}, "EXTEND_EXCEPTION",
               {"new_expires_at": (T0 + timedelta(days=10)).isoformat(), "reason": "x"})
    assert r.status_code == 422 and "tối đa" in r.text


def test_expired_exception_with_real_traffic_is_low_urgency(client, clock, db_path):
    exc_id = client.post("/exceptions", json=declaration()).json()["exception_id"]
    _seed_strong_edge(db_path, "checkoutservice", "paymentservice", 50051)
    clock.now = T0 + timedelta(days=3)
    item = queue(client, "EMERGENCY_EXCEPTION_EXPIRED")[0]
    assert item["target_ref"]["exception_id"] == exc_id
    assert item["traffic_support"] is True and item["urgency"] == "low"


def _seed_strong_edge(db_path, src, dst, port):
    from orchestration.run_log import PipelineRunLog
    from tier1 import storage as tier1_storage
    from tier1.schema import DependencyEdgeResult, DependencyStatus, IdentityKey, Scope, ServiceRef, TrafficClass

    conn = open_db(db_path)
    log = PipelineRunLog(conn, "trial-01", "trial-01.a1")
    log.record("trial", "RUNNING")
    edge = DependencyEdgeResult(
        identity_key=IdentityKey(NS, src, src, NS, dst, "GRPC", port), destination_service_ref=ServiceRef(dst, NS),
        scope=Scope.IN_CLUSTER, traffic_class=TrafficClass.BUSINESS, count=7000, distinct_days=7, error_ratio=0.0,
        unstable=False, config_finding=None, score=0.97, dependency_status=DependencyStatus.OBSERVED_STRONG,
        snapshot_id="demo")
    tier1_storage.insert_results(conn, [edge], run_id="trial-01", attempt_id="trial-01.a1")
    log.record("trial", "SUCCESS")
    conn.close()


# ---------------------------------------------------------------------------
# Audit Log -> UNATTRIBUTED_CONFIG_CHANGE
# ---------------------------------------------------------------------------


def test_audit_parser_keeps_only_completed_successful_policy_mutations():
    keep = audit_event()
    assert parse_audit_event(keep).change_type == "CREATE"
    assert parse_audit_event(audit_event(stage="RequestReceived")) is None
    assert parse_audit_event(audit_event(verb="get")) is None
    assert parse_audit_event(audit_event(resource="configmaps")) is None
    assert parse_audit_event(audit_event(code=403)) is None
    assert parse_audit_event(audit_event(verb="patch")).change_type == "UPDATE"
    assert parse_audit_event(audit_event(user="system:serviceaccount:argocd:argocd-application-controller")).source == "GITOPS"
    assert parse_audit_event(audit_event(labels={"kltn.zt/exception-id": "exc_1"})).exception_label == "exc_1"
    lines = [json.dumps({"kind": "EventList", "items": [keep, audit_event(audit_id="a2", verb="delete")]}), "{hỏng", ""]
    assert [c.change_type for c in parse_audit_log(lines)] == ["CREATE", "DELETE"]


def test_imperative_change_without_exception_is_unattributed_until_decided(client, db_path, clock):
    assert ingest_audit(db_path, audit_event(audit_id="a1", name="allow-all")) == 1
    assert ingest_audit(db_path, audit_event(audit_id="a1", name="allow-all")) == 0      # nạp lại không trùng
    items = queue(client, "UNATTRIBUTED_CONFIG_CHANGE")
    assert [i["target_ref"] for i in items] == [{"resource_kind": "NetworkPolicy", "namespace": NS, "name": "allow-all"}]
    assert items[0]["change"]["actor"] == "kubernetes-admin"

    clock.now = T0 + timedelta(hours=1)
    decide(client, "UNATTRIBUTED_CONFIG_CHANGE", items[0]["target_ref"], "REVOKE", {})
    assert queue(client, "UNATTRIBUTED_CONFIG_CHANGE") == []
    # sửa lại lần nữa SAU quyết định -> nổi lên lại
    ingest_audit(db_path, audit_event(audit_id="a2", verb="patch", name="allow-all", at=T0 + timedelta(days=2)))
    assert len(queue(client, "UNATTRIBUTED_CONFIG_CHANGE")) == 1


def test_gitops_and_declared_exception_changes_are_attributed(client, db_path, clock):
    exc = client.post("/exceptions", json=declaration()).json()
    ingest_audit(db_path,
                 audit_event(audit_id="g1", name="baseline", user="system:serviceaccount:flux-system:kustomize-controller"),
                 audit_event(audit_id="e1", name=exc["resource_name"]),                         # theo tên (mức Metadata)
                 audit_event(audit_id="e2", name="tu-dat-ten", labels={"kltn.zt/exception-id": exc["exception_id"]}))
    changes = {c["audit_id"]: c["attributed_to"] for c in client.get("/config-changes").json()}
    assert changes == {"g1": "gitops", "e1": f"exception:{exc['exception_id']}",
                       "e2": f"exception:{exc['exception_id']}"}
    assert queue(client, "UNATTRIBUTED_CONFIG_CHANGE") == []


def test_change_after_exception_expired_is_unattributed_but_delete_is_fine(client, db_path):
    exc = client.post("/exceptions", json=declaration()).json()
    late = T0 + timedelta(days=3)
    ingest_audit(db_path, audit_event(audit_id="u1", verb="patch", name=exc["resource_name"], at=late),
                 audit_event(audit_id="d1", verb="delete", name=exc["resource_name"], ns="other-ns", at=late))
    attributed = {c["audit_id"]: c["attributed_to"] for c in client.get("/config-changes").json()}
    assert attributed["u1"] is None                     # sửa policy ngoại lệ khi đã quá hạn
    assert attributed["d1"] is None                     # khác namespace -> không phải policy của ngoại lệ
    ingest_audit(db_path, audit_event(audit_id="d2", verb="delete", name=exc["resource_name"], at=late))
    assert {c["audit_id"]: c["attributed_to"] for c in client.get("/config-changes").json()}["d2"].startswith("exception:")


# ---------------------------------------------------------------------------
# Kết nối bị chặn -> BLOCKED_CONNECTION
# ---------------------------------------------------------------------------

CILIUM = {"flow": {"time": "2026-11-19T03:10:00Z", "verdict": "DROPPED", "drop_reason_desc": "POLICY_DENIED",
                   "traffic_direction": "INGRESS", "IP": {"source": "10.0.0.5", "destination": "10.0.0.9"},
                   "source": {"namespace": NS, "pod_name": "checkout-7d9", "workloads": [{"name": "checkoutservice"}]},
                   "destination": {"namespace": NS, "pod_name": "pay-1", "workloads": [{"name": "paymentservice"}]},
                   "l4": {"TCP": {"source_port": 40000, "destination_port": 50051}}}}
CALICO = {"start_time": "2026-11-19T03:00:00Z", "end_time": "2026-11-19T03:05:00Z", "action": "deny",
          "source_namespace": NS, "source_name_aggr": "checkoutservice-*", "dest_namespace": NS,
          "dest_name_aggr": "paymentservice-*", "dest_port": 50051, "proto": "tcp", "num_flows": 3}
ZTUNNEL = {"_time": "2026-11-19T03:20:00Z", "src.namespace": NS, "src.workload": "checkoutservice",
           "src.identity": f"spiffe://cluster.local/ns/{NS}/sa/checkoutservice", "dst.namespace": NS,
           "dst.workload": "paymentservice", "dst.hbone_addr": "10.0.0.9:50051",
           "error": "connection closed due to policy rejection: explicitly denied by: shared-eval/deny-pay"}
GENERIC = {"time": "2026-11-19T03:30:00Z", "source_namespace": NS, "source_workload": "checkoutservice",
           "destination_namespace": NS, "destination_workload": "paymentservice", "protocol": "tcp", "port": 50051}


@pytest.mark.parametrize("fmt,obj,ignored", [
    ("cilium", CILIUM, {"flow": {**CILIUM["flow"], "verdict": "FORWARDED"}}),
    ("calico", CALICO, {**CALICO, "action": "allow"}),
    ("ztunnel", ZTUNNEL, {**ZTUNNEL, "error": ""}),
    ("generic", GENERIC, {"source_namespace": NS}),
])
def test_each_format_normalises_to_same_flow_key(fmt, obj, ignored):
    (conn,) = parse_blocked_log([json.dumps(obj), json.dumps(ignored), "rác"], fmt)
    assert conn.reporter == fmt and conn.port == 50051 and conn.protocol == "TCP"
    if fmt != "calico":
        assert conn.flow_key == f"{NS}|checkoutservice|{NS}|paymentservice|TCP|50051"


def test_unknown_format_rejected():
    with pytest.raises(ValueError):
        parse_blocked_log([], "nftables")


def test_blocked_connection_flow_open_exception_then_reblock(client, db_path, clock):
    lines = [json.dumps(CILIUM), json.dumps({"flow": {**CILIUM["flow"], "time": "2026-11-19T03:11:00Z"}})]
    assert ingest_blocked(db_path, lines, "cilium") == 2
    assert ingest_blocked(db_path, lines, "cilium") == 0                          # khử trùng
    (item,) = queue(client, "BLOCKED_CONNECTION")
    assert item["blocked"]["block_count"] == 2 and item["urgency"] == "high"
    flow_key = item["target_ref"]["flow_key"]

    # hợp lệ -> phải mở ngoại lệ thật trước, rồi mới APPROVE được
    assert decide(client, "BLOCKED_CONNECTION", {"flow_key": flow_key}, "APPROVE", {}).status_code == 422
    exc_id = client.post("/exceptions", json=declaration(blocked_flow_key=flow_key)).json()["exception_id"]
    assert decide(client, "BLOCKED_CONNECTION", {"flow_key": flow_key}, "APPROVE",
                  {"exception_id": exc_id}).status_code == 201
    assert queue(client, "BLOCKED_CONNECTION") == []
    # bị chặn lại SAU quyết định -> báo lại
    ingest_blocked(db_path, [json.dumps({"flow": {**CILIUM["flow"], "time": "2099-01-01T00:00:00Z"}})], "cilium")
    assert len(queue(client, "BLOCKED_CONNECTION")) == 1
    assert len(client.get("/blocked-connections", params={"flow_key": flow_key}).json()) == 3


def test_ztunnel_denied_pulled_from_victorialogs():
    q = build_ztunnel_denied_query(T0 - timedelta(hours=24), T0)
    assert '"policy rejection"' in q and "unpack_logfmt" in q and "ztunnel" in q
    got = collect_ztunnel_denied(FakeLogsClient([ZTUNNEL, {**ZTUNNEL, "error": ""}]), T0 - timedelta(hours=1), T0)
    assert [(c.source_service_account, c.port) for c in got] == [("checkoutservice", 50051)]


# ---------------------------------------------------------------------------
# CLI sweep + migration DB cũ
# ---------------------------------------------------------------------------


def test_cli_sweep_ingests_and_reports_exit_2_when_high_items(tmp_path, infra_files):
    audit = tmp_path / "audit.log"
    audit.write_text(json.dumps(audit_event(name="allow-all")) + "\n", encoding="utf-8")
    flows = tmp_path / "flows.json"
    flows.write_text(json.dumps(GENERIC) + "\n", encoding="utf-8")
    args = cli_continuity.build_parser().parse_args(
        ["--db", str(infra_files["db_path"]), "--audit-log", str(audit),
         "--blocked-log", str(flows), "--blocked-format", "generic"])
    summary = cli_continuity.run(args, logs_client=FakeLogsClient([ZTUNNEL]), now=T0)
    assert summary["config_changes_added"] == 1 and summary["blocked_events_added"] == 2
    assert {i["reason_code"] for i in summary["open_items"]} == {"UNATTRIBUTED_CONFIG_CHANGE", "BLOCKED_CONNECTION"}
    assert cli_continuity.main(["--db", str(infra_files["db_path"])]) == 2
    assert cli_continuity.main(["--db", str(infra_files["db_path"]), "--blocked-log", "x"]) == 1


def test_old_db_with_narrow_reason_check_is_migrated_without_losing_decisions(tmp_path):
    path = tmp_path / "old.sqlite3"
    old_codes = "'STALE_PERMISSION', 'CLEAN_CONFIG_MATCH'"
    old_schema = ORCHESTRATION_SCHEMA.split("CREATE TABLE IF NOT EXISTS human_decisions (", 1)[1].split(");", 1)[0]
    old_schema = old_schema.split("CHECK (reason_code IN (", 1)[0] + f"CHECK (reason_code IN ({old_codes}))" + \
        old_schema.split("CHECK (reason_code IN (", 1)[1].split("))", 1)[1]
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE human_decisions ({old_schema})")
    conn.execute("INSERT INTO human_decisions (decision_id, reason_code, target_ref, decided_by, decided_at, action) "
                 "VALUES ('dec_old', 'STALE_PERMISSION', '{}', 'bin', '2026-10-01', 'REVOKE')")
    conn.commit()
    conn.close()
    conn = open_db(path)
    assert [r[0] for r in conn.execute("SELECT decision_id FROM human_decisions")] == ["dec_old"]
    conn.execute("INSERT INTO human_decisions (decision_id, reason_code, target_ref, decided_by, decided_at, action) "
                 "VALUES ('dec_new', 'BLOCKED_CONNECTION', '{}', 'bin', '2026-11-19', 'REJECT')")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM human_decisions")
    conn.execute("SELECT * FROM dependency_current_state").fetchall()   # view dựng lại được
    conn.close()
