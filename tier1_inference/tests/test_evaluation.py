"""evaluation.py — TIER1_DESIGN.md mục 13.3 (precision/recall/F1, CI 95%, Wilcoxon)."""

from __future__ import annotations

import pytest

from tier1.evaluation import (
    filter_ground_truth,
    mean_ci95,
    precision_recall_f1,
    predicted_positive_pairs,
    wilcoxon_signed_rank,
)
from tier1.schema import DependencyEdgeResult, DependencyStatus, Scope, TrafficClass

from .conftest import make_identity_key

NS = "default"


def _result(dest="productcatalogservice", status=DependencyStatus.OBSERVED_STRONG, traffic_class=TrafficClass.BUSINESS, distinct_days=5) -> DependencyEdgeResult:
    key = make_identity_key(dest=dest)
    return DependencyEdgeResult(
        identity_key=key,
        destination_service_ref=None,
        scope=Scope.IN_CLUSTER,
        traffic_class=traffic_class,
        count=100,
        distinct_days=distinct_days,
        error_ratio=0.0,
        unstable=False,
        config_finding=None,
        score=0.9,
        dependency_status=status,
        snapshot_id="snap_test",
    )


# ---------------------------------------------------------------------------
# predicted_positive_pairs / precision_recall_f1
# ---------------------------------------------------------------------------


def test_predicted_positive_only_counts_strong():
    results = [
        _result(dest="a", status=DependencyStatus.OBSERVED_STRONG),
        _result(dest="b", status=DependencyStatus.OBSERVED_WEAK),
        _result(dest="c", status=DependencyStatus.NOT_OBSERVED),
    ]
    predicted = predicted_positive_pairs(results)
    assert len(predicted) == 1
    assert list(predicted)[0][3] == "a"  # destination_canonical_service ở vị trí thứ 4 của pair_key


def test_precision_recall_perfect_match():
    results = [_result(dest="a", status=DependencyStatus.OBSERVED_STRONG)]
    gt = {make_identity_key(dest="a").pair_key(): True}
    m = precision_recall_f1(results, gt)
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.f1 == 1.0
    assert (m.tp, m.fp, m.fn) == (1, 0, 0)


def test_precision_recall_false_positive():
    """B1-style: predict STRONG cho 1 cặp noise không có trong ground truth thật."""
    results = [_result(dest="noise", status=DependencyStatus.OBSERVED_STRONG)]
    gt = {make_identity_key(dest="real").pair_key(): True}
    m = precision_recall_f1(results, gt)
    assert m.precision == 0.0
    assert m.recall == 0.0
    assert (m.tp, m.fp, m.fn) == (0, 1, 1)


def test_precision_recall_false_negative():
    """B2-style: bỏ sót 1 dependency thật (rớt xuống NOT_OBSERVED thay vì STRONG)."""
    results = [_result(dest="a", status=DependencyStatus.NOT_OBSERVED)]
    gt = {make_identity_key(dest="a").pair_key(): True}
    m = precision_recall_f1(results, gt)
    assert m.recall == 0.0
    assert (m.tp, m.fp, m.fn) == (0, 0, 1)


def test_precision_recall_no_predictions_no_ground_truth_positive_is_zero_not_error():
    m = precision_recall_f1([], {})
    assert (m.precision, m.recall, m.f1) == (0.0, 0.0, 0.0)


def test_precision_ignores_ground_truth_negative_pairs():
    """Cặp ground_truth=False (biết là noise/không thật) không được tính là actual_positive."""
    results = [_result(dest="noise", status=DependencyStatus.OBSERVED_STRONG)]
    gt = {make_identity_key(dest="noise").pair_key(): False}
    m = precision_recall_f1(results, gt)
    assert m.precision == 0.0  # dự đoán dương nhưng ground truth nói là âm -> FP
    assert (m.tp, m.fp, m.fn) == (0, 1, 0)


# ---------------------------------------------------------------------------
# filter_ground_truth — lát cắt mục 13.3
# ---------------------------------------------------------------------------


def test_filter_ground_truth_sparse_traffic_slice():
    results = [
        _result(dest="sparse", distinct_days=1),
        _result(dest="dense", distinct_days=7),
    ]
    gt = {
        make_identity_key(dest="sparse").pair_key(): True,
        make_identity_key(dest="dense").pair_key(): True,
    }
    sliced = filter_ground_truth(gt, results, predicate=lambda r: r.distinct_days <= 2)
    assert set(sliced.keys()) == {make_identity_key(dest="sparse").pair_key()}


def test_filter_ground_truth_healthcheck_slice():
    results = [
        _result(dest="hc", traffic_class=TrafficClass.HEALTHCHECK),
        _result(dest="biz", traffic_class=TrafficClass.BUSINESS),
    ]
    gt = {
        make_identity_key(dest="hc").pair_key(): False,
        make_identity_key(dest="biz").pair_key(): True,
    }
    sliced = filter_ground_truth(gt, results, predicate=lambda r: r.traffic_class == TrafficClass.HEALTHCHECK)
    assert set(sliced.keys()) == {make_identity_key(dest="hc").pair_key()}


# ---------------------------------------------------------------------------
# mean_ci95
# ---------------------------------------------------------------------------


def test_mean_ci95_basic():
    values = [0.8, 0.85, 0.9, 0.75, 0.82]
    ci = mean_ci95(values)
    assert ci.low < ci.mean < ci.high
    assert ci.half_width > 0


def test_mean_ci95_single_value_zero_width():
    ci = mean_ci95([0.5])
    assert ci.mean == 0.5
    assert ci.half_width == 0.0


def test_mean_ci95_zero_variance_zero_width():
    ci = mean_ci95([0.7, 0.7, 0.7])
    assert ci.half_width == 0.0
    assert ci.mean == pytest.approx(0.7)


def test_mean_ci95_empty_raises():
    with pytest.raises(ValueError):
        mean_ci95([])


# ---------------------------------------------------------------------------
# wilcoxon_signed_rank
# ---------------------------------------------------------------------------


def test_wilcoxon_identical_values_p_is_one():
    values = [0.8] * 15
    result = wilcoxon_signed_rank(values, values)
    assert result.p_value == 1.0


def test_wilcoxon_length_mismatch_raises():
    with pytest.raises(ValueError):
        wilcoxon_signed_rank([0.1, 0.2], [0.1])


def test_wilcoxon_empty_returns_none():
    result = wilcoxon_signed_rank([], [])
    assert result.p_value is None


def test_wilcoxon_real_difference_produces_valid_p_value():
    tier1_recall = [0.9, 0.92, 0.88, 0.95, 0.91, 0.89, 0.93, 0.9, 0.94, 0.87, 0.92, 0.9, 0.91, 0.93, 0.89]
    b2_recall = [0.5, 0.55, 0.45, 0.6, 0.52, 0.48, 0.58, 0.5, 0.56, 0.44, 0.53, 0.5, 0.51, 0.57, 0.46]
    result = wilcoxon_signed_rank(tier1_recall, b2_recall)
    assert 0.0 <= result.p_value <= 1.0
    assert result.p_value < 0.05  # khác biệt rõ ràng, kỳ vọng có ý nghĩa thống kê
