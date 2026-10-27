"""Các quyết định Tầng 1 chốt 15/10/2026 — kiểm end-to-end qua `run_tier1`
(TIER1_DESIGN.md mục 1b, 2b, 5 nhánh nhiễu, 7, 10b; TIER1_CHANGE_PLAN.md L1–L11).

Dùng lại đúng ví dụ đã thống nhất khi thiết kế (stage 3, 4, 5)."""

from dataclasses import replace

import pytest

from tier1 import storage
from tier1.aggregation import aggregate_series
from tier1.candidates import EXACT, PAIR_ONLY, PORT_ONLY, Candidate, discover_candidates, match_config_to_edges
from tier1.classification import classify_all
from tier1.config import ScoringConfig
from tier1.pipeline import run_tier1
from tier1.schema import (
    ConfigEvidenceRecord,
    ConfigFinding,
    DependencyStatus,
    EvidenceRef,
    Scope,
    ServiceRef,
    TrafficClass,
)

from .conftest import make_identity_key, make_series

NS = "trial-01"


def ev(src, dst, port, *, protocol=None, finding=ConfigFinding.FOUND_STRUCTURAL, item_id="i0001",
       sa=None, raw_host=None) -> ConfigEvidenceRecord:
    unresolved = dst is None
    return ConfigEvidenceRecord(
        source_namespace=NS, source_canonical_service=src, source_service_account=sa or src,
        destination_namespace=None if unresolved else NS, destination_canonical_service=dst,
        destination_service_account=None, protocol=protocol, port=port, port_unknown=port is None,
        config_finding=finding, evidence_source="env", scope=Scope.UNKNOWN if unresolved else Scope.IN_CLUSTER,
        snapshot_id="s", collected_at="t", raw_host=raw_host or (dst or "x"),
        evidence_refs=(EvidenceRef(item_id=item_id, source_kind="env", container="c", key="K", quote="q"),),
    )


def series(src, dst, *, port, protocol="GRPC", path="/svc.X/Call", count=100, days=None, sa=None, flags=(), **kw):
    s = make_series(source=src, dest=dst, source_ns=NS, dest_ns=NS, source_workload_namespace=NS,
                    port=port, protocol=protocol, path=path, count=count,
                    traffic_days=days if days is not None else ["2026-10-07"], **kw)
    s.source_service_account = sa or src
    for f in flags:
        s.add_flag(f)
    return s


def by_dest(results, dst, port="any"):
    return [r for r in results if r.identity_key.destination_canonical_service == dst
            and (port == "any" or r.identity_key.port == port)]


# --------------------------------------------------------------------------- stage 3: khớp config

def test_unresolved_destinations_of_same_source_are_separate_rows():
    """L4: bản cũ dồn mọi đích UNRESOLVED của 1 nguồn vào 1 ô -> mất `old-ledger`."""
    results = run_tier1([], [
        ev("reportjob", None, 8443, finding=ConfigFinding.FOUND_UNRESOLVED, raw_host="billing-legacy.internal"),
        ev("reportjob", None, 9000, finding=ConfigFinding.FOUND_UNRESOLVED, raw_host="old-ledger"),
    ], "s")
    dests = sorted(r.identity_key.destination_canonical_service for r in results)
    assert dests == ["raw:billing-legacy.internal", "raw:old-ledger"]
    assert all(r.escalate_to_tier2 and r.dependency_status == DependencyStatus.NOT_OBSERVED for r in results)


def test_config_on_port_without_traffic_becomes_its_own_not_observed_row():
    """L5: redis 6379 có traffic, sentinel 26379 chỉ có config -> 26379 thành dòng riêng."""
    results = run_tier1(
        [series("checkoutservice", "redis-cart", port=6379, protocol="TCP", path=None)],
        [ev("checkoutservice", "redis-cart", 6379, item_id="i0001"),
         ev("checkoutservice", "redis-cart", 26379, item_id="i0002")],
        "s",
    )
    main = by_dest(results, "redis-cart", 6379)[0]
    leftover = by_dest(results, "redis-cart", 26379)[0]
    assert main.config_match_level == EXACT
    assert leftover.dependency_status == DependencyStatus.NOT_OBSERVED and leftover.needs_human_review


def test_match_levels_and_protocol_mismatch_flag():
    results = run_tier1([
        series("frontend", "productcatalogservice", port=3550),
        series("frontend", "cartservice", port=7070, protocol="HTTP", path="/cart"),
        series("frontend", "shippingservice", port=None, port_source="unknown"),
    ], [
        ev("frontend", "productcatalogservice", 3550),             # config không ghi protocol
        ev("frontend", "cartservice", 7070, protocol="GRPC"),      # lệch protocol
        ev("frontend", "shippingservice", None),                   # cả hai thiếu port (27/10: config có
                                                                   # đúng 1 port thì cạnh lấy port đó)
    ], "s")
    assert by_dest(results, "productcatalogservice")[0].config_match_level == EXACT
    cart = by_dest(results, "cartservice")[0]
    assert cart.config_match_level == PORT_ONLY and "protocol_mismatch" in cart.flags
    assert cart.config_finding == ConfigFinding.FOUND_STRUCTURAL  # strength không đổi
    assert by_dest(results, "shippingservice")[0].config_match_level == PAIR_ONLY


def test_sa_mismatch_flag_when_config_identity_differs_from_traffic():
    results = run_tier1([series("frontend", "cartservice", port=7070, sa="frontend")],
                        [ev("frontend", "cartservice", 7070, sa="other-sa")], "s")
    assert "sa_mismatch" in results[0].flags


def test_config_choice_is_deterministic_by_item_id():
    s = series("frontend", "cartservice", port=None, port_source="unknown")
    classify_all([s])
    cand = discover_candidates(aggregate_series([s]), [
        ev("frontend", "cartservice", None, finding=ConfigFinding.FOUND_EMBEDDED, item_id="i0009"),
        ev("frontend", "cartservice", None, finding=ConfigFinding.FOUND_EMBEDDED, item_id="i0002"),
    ])
    m = match_config_to_edges(next(iter(cand.values())))
    assert next(iter(m.per_edge.values())).evidence.sort_key == "i0002"


# --------------------------------------------------------------------------- stage 2: gộp ngày

def test_distinct_days_is_union_of_days_across_paths():
    """L8: /export ngày 02 + /sync ngày 05 = 2 ngày (bản cũ: max(1,1) = 1)."""
    results = run_tier1([
        series("reportjob", "reportservice", port=8080, protocol="HTTP", path="/export", count=3, days=["2026-10-02"]),
        series("reportjob", "reportservice", port=8080, protocol="HTTP", path="/sync", count=2, days=["2026-10-05"]),
    ], [], "s")
    r = results[0]
    assert r.distinct_days == 2 and r.traffic_days == ["2026-10-02", "2026-10-05"] and r.count == 5


def test_legacy_series_without_days_falls_back_to_max_with_flag():
    s1 = series("a", "b", port=1, days=[], distinct_days=3)
    s2 = series("a", "b", port=1, path="/other", days=[], distinct_days=2)
    r = run_tier1([s1, s2], [], "s")[0]
    assert r.distinct_days == 3 and "legacy_days" in r.flags


# --------------------------------------------------------------------------- stage 5: nhánh nhiễu

def test_healthcheck_only_edge_goes_to_human_review_not_tier2():
    """L6: bản cũ: điểm ~0,47 -> WEAK -> gọi Tier 2 chỉ để kết luận 'là healthcheck'."""
    r = run_tier1([series("harness-hc", "frontend", port=8080, protocol="HTTP", path="/_healthz",
                          count=50, days=[f"2026-10-0{d}" for d in range(2, 9)], flags=["probe_path"])], [], "s")[0]
    assert r.traffic_class == TrafficClass.HEALTHCHECK
    assert not r.escalate_to_tier2 and r.needs_human_review and "noise_only" in r.flags


def test_verified_monitoring_only_edge_is_auto_allowed():
    s = series("prometheus", "checkoutservice", port=8080, path="/metrics", sa="prometheus", count=80)
    s.add_flag("verified_monitoring")
    r = run_tier1([s], [], "s")[0]
    assert r.traffic_class == TrafficClass.MONITORING
    assert r.auto_allow_reason == "VERIFIED_MONITORING"
    assert not r.escalate_to_tier2 and not r.needs_human_review
    assert r.dependency_status != DependencyStatus.OBSERVED_STRONG  # không tính là "tự kết luận"


def test_edge_mixing_business_and_healthcheck_is_scored_normally():
    r = run_tier1([
        series("frontend", "productcatalogservice", port=3550, path="/svc.Catalog/List", count=1200,
               days=[f"2026-10-0{d}" for d in range(2, 9)]),
        series("frontend", "productcatalogservice", port=3550, path="/grpc.health.v1.Health/Check", count=300,
               days=[f"2026-10-0{d}" for d in range(2, 9)]),
    ], [ev("frontend", "productcatalogservice", 3550)], "s")[0]
    assert r.traffic_class == TrafficClass.BUSINESS and "noise_only" not in r.flags
    assert r.dependency_status == DependencyStatus.OBSERVED_STRONG


def test_claimed_monitoring_name_is_not_auto_allowed():
    s = series("prometheus", "paymentservice", port=50051, path="/svc.Payment/Charge", sa="default", count=5)
    r = run_tier1([s], [], "s")[0]
    assert r.traffic_class == TrafficClass.BUSINESS and "claimed_monitoring" in r.flags
    assert r.auto_allow_reason is None


def test_low_count_strong_is_flagged_for_review():
    """count_floor không dùng (chốt 15/10); thay vào đó đánh dấu STRONG có count < 3."""
    r = run_tier1([series("a", "b", port=80, count=1, last_seen_days_ago=0.0)],
                  [ev("a", "b", 80)], "s", config=ScoringConfig(volume_reference=10))[0]
    assert r.dependency_status == DependencyStatus.OBSERVED_STRONG
    assert "low_count_strong" in r.flags


# --------------------------------------------------------------------------- DNS, scope

def test_dns_split_by_traffic_class_and_auto_allowed():
    s = series("frontend", "kube-dns", port=None, protocol="TCP", path=None,
               destination_service_ref=ServiceRef("kube-dns", "kube-system"))
    r = run_tier1([s], [], "s")[0]
    assert r.traffic_class == TrafficClass.DNS and r.dns_auto_allow


def test_passthrough_destination_is_external_and_skips_selector_check():
    s = series("checkoutservice", "unknown", port=None, protocol="TCP", path=None,
               destination_service_ref=None, count=500, days=[f"2026-10-0{d}" for d in range(2, 9)])
    s.add_flag("external_unregistered")
    r = run_tier1([s], [], "s", config=ScoringConfig(volume_reference=500))[0]
    assert r.scope == Scope.EXTERNAL
    assert r.dependency_status != DependencyStatus.NEEDS_MANUAL_SELECTOR


# --------------------------------------------------------------------------- stage 6: lưu trữ

def _strong_edge():
    return series("frontend", "cartservice", port=7070, count=5000, days=[f"2026-10-0{d}" for d in range(2, 9)])


def test_rerun_same_snapshot_does_not_read_its_own_result(tmp_path):
    """L3: chạy lại snapshot X phải cho ra đúng kết quả lần đầu."""
    conn = storage.connect(tmp_path / "db.sqlite")
    first = run_tier1([_strong_edge()], [], "snap-X", conn=conn, snapshot_time="2026-10-08T00:00:00Z")
    again = run_tier1([_strong_edge()], [], "snap-X", conn=conn, snapshot_time="2026-10-08T00:00:00Z")
    assert first[0].previous_status is None and again[0].previous_status is None


def test_history_is_per_service_account(tmp_path):
    """L2: 2 cạnh chỉ khác SA không dùng chung lịch sử."""
    conn = storage.connect(tmp_path / "db.sqlite")
    run_tier1([_strong_edge()], [], "snap-1", conn=conn, snapshot_time="2026-10-07T00:00:00Z")
    other_sa = _strong_edge()
    other_sa.source_service_account = "other-sa"
    r = run_tier1([other_sa], [], "snap-2", conn=conn, snapshot_time="2026-10-08T00:00:00Z")[0]
    assert r.previous_status is None
    same = run_tier1([_strong_edge()], [], "snap-3", conn=conn, snapshot_time="2026-10-09T00:00:00Z")[0]
    assert same.previous_status == DependencyStatus.OBSERVED_STRONG


def test_history_uses_data_time_not_run_time(tmp_path):
    """Ablation 7 snapshot chạy trong vài phút: snapshot ngày 3 chỉ nhìn ngày 1-2."""
    conn = storage.connect(tmp_path / "db.sqlite")
    weak = series("a", "b", port=1, count=1, days=["2026-10-01"], last_seen_days_ago=6)
    run_tier1([weak], [], "d1", conn=conn, snapshot_time="2026-10-01T23:59:59Z")
    run_tier1([_strong_edge()], [], "d9", conn=conn, snapshot_time="2026-10-09T23:59:59Z")
    r = run_tier1([weak], [], "d2", conn=conn, snapshot_time="2026-10-02T23:59:59Z")[0]
    assert r.previous_status == DependencyStatus.OBSERVED_WEAK  # không nhìn thấy snapshot ngày 9


def test_new_columns_are_persisted(tmp_path):
    conn = storage.connect(tmp_path / "db.sqlite")
    s = _strong_edge()
    s.port_source = "inferred_single_port"
    run_tier1([s], [ev("frontend", "cartservice", 7070)], "snap", conn=conn, snapshot_time="2026-10-08T00:00:00Z")
    row = conn.execute("SELECT snapshot_time, port_source, traffic_days, config_match_level FROM dependency_evidence").fetchone()
    assert row[0] == "2026-10-08 00:00:00" and row[1] == "inferred_single_port"
    assert row[2].startswith('["2026-10-02"') and row[3] == EXACT
