"""baseline.py — TIER1_DESIGN.md mục 13.1 (B1)/13.2 (B2)."""

from __future__ import annotations

from tier1.baseline import run_baseline_b1, run_baseline_b2
from tier1.schema import ConfigFinding, DependencyStatus, Scope

from .conftest import make_config_evidence, make_series

NS = "default"


# ---------------------------------------------------------------------------
# B1 — mục 13.1
# ---------------------------------------------------------------------------


def test_b1_any_traffic_becomes_strong_regardless_of_class(config):
    """mục 13.1: "không phân biệt traffic_class" — kể cả HEALTHCHECK/MONITORING vẫn
    thành OBSERVED_STRONG dưới B1 (đây chính là điểm B1 dự kiến precision thấp)."""
    series = [make_series(count=50, distinct_days=5, path="/healthz")]
    results = run_baseline_b1(series, [], snapshot_id="snap_b1")
    assert len(results) == 1
    assert results[0].dependency_status == DependencyStatus.OBSERVED_STRONG


def test_b1_low_volume_still_strong():
    """mục 13.1: count>0 là ĐỦ, không cần distinct_days/volume tối thiểu nào."""
    series = [make_series(count=1, distinct_days=1)]
    results = run_baseline_b1(series, [], snapshot_id="snap_b1")
    assert results[0].dependency_status == DependencyStatus.OBSERVED_STRONG


def test_b1_potential_only_same_as_tier1_not_observed():
    """Cặp chỉ có config evidence (chưa có traffic) -> nhánh NOT_OBSERVED y hệt Tier 1."""
    ev = make_config_evidence(finding=ConfigFinding.FOUND_EMBEDDED)
    results = run_baseline_b1([], [ev], snapshot_id="snap_b1")
    assert len(results) == 1
    assert results[0].dependency_status == DependencyStatus.NOT_OBSERVED
    assert results[0].needs_human_review is True
    assert results[0].escalate_to_tier2 is False


def test_b1_no_manual_selector_status_possible():
    """B1 không chạy selector validation (mục 3b không thuộc phạm vi so sánh mục 13)."""
    series = [make_series(count=100, distinct_days=7)]
    results = run_baseline_b1(series, [], snapshot_id="snap_b1")
    assert all(r.dependency_status != DependencyStatus.NEEDS_MANUAL_SELECTOR for r in results)


# ---------------------------------------------------------------------------
# B2 — mục 13.2
# ---------------------------------------------------------------------------


def test_b2_meets_threshold_is_strong_with_fixed_score():
    series = [make_series(count=20, distinct_days=3)]
    results = run_baseline_b2(series, [], snapshot_id="snap_b2")
    assert results[0].dependency_status == DependencyStatus.OBSERVED_STRONG
    assert results[0].score == 0.9


def test_b2_below_count_threshold_escalates_when_no_config_evidence():
    """Dưới ngưỡng cứng, không có config evidence -> mù cả 2 nguồn -> escalate Tier 2
    (mục 5: NOT_OBSERVED_BLIND), đúng "xử lý như NOT_OBSERVED/escalate theo mục 5"."""
    series = [make_series(count=19, distinct_days=5)]
    results = run_baseline_b2(series, [], snapshot_id="snap_b2")
    assert results[0].dependency_status == DependencyStatus.NOT_OBSERVED
    assert results[0].escalate_to_tier2 is True
    assert results[0].needs_human_review is False


def test_b2_below_days_threshold_with_clean_config_needs_human_review():
    """Dưới ngưỡng vì distinct_days (traffic thưa, mục 13.3), NHƯNG có config evidence
    sạch cho cùng cặp -> hàng đợi review người, không escalate (mục 5 nhánh sạch)."""
    series = [make_series(count=100, distinct_days=2)]
    ev = make_config_evidence(finding=ConfigFinding.FOUND_STRUCTURAL, port=3550, protocol="GRPC")
    results = run_baseline_b2(series, [ev], snapshot_id="snap_b2")
    assert results[0].dependency_status == DependencyStatus.NOT_OBSERVED
    assert results[0].needs_human_review is True
    assert results[0].escalate_to_tier2 is False


def test_b2_potential_only_same_as_tier1_not_observed():
    ev = make_config_evidence(finding=ConfigFinding.FOUND_UNRESOLVED)
    results = run_baseline_b2([], [ev], snapshot_id="snap_b2")
    assert results[0].dependency_status == DependencyStatus.NOT_OBSERVED
    assert results[0].escalate_to_tier2 is True


def test_b2_no_hysteresis_previous_status_always_none():
    """mục 13.1/13.2: B1/B2 bỏ qua hoàn toàn hysteresis — previous_status luôn None dù
    gọi nhiều lần liên tiếp (không đọc/ghi lịch sử)."""
    series = [make_series(count=25, distinct_days=4)]
    r1 = run_baseline_b2(series, [], snapshot_id="snap_1")
    r2 = run_baseline_b2(series, [], snapshot_id="snap_2")
    assert r1[0].previous_status is None
    assert r2[0].previous_status is None


# ---------------------------------------------------------------------------
# DNS — auto-allow giống nhau ở cả 2 baseline (mục 7, không thuộc phạm vi mục 13)
# ---------------------------------------------------------------------------


def test_dns_handled_same_in_both_baselines():
    dns_series = [make_series(dest="kube-dns", dest_ns="kube-system", port=53, protocol="DNS", count=10)]
    r1 = run_baseline_b1(dns_series, [], snapshot_id="snap_dns")
    r2 = run_baseline_b2(dns_series, [], snapshot_id="snap_dns")
    assert r1[0].dns_auto_allow is True
    assert r2[0].dns_auto_allow is True
