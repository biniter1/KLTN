"""scoring.py — công thức sigmoid, percentile_95 (EXPECTED_MAX), feature computation — mục 4/4a."""

import pytest

from tier1.aggregation import TrafficAggregate
from tier1.config import ScoringConfig
from tier1.schema import ConfigFinding, IdentityKey, TrafficClass
from tier1.scoring import (
    CONFIG_MATCH_STRENGTH,
    ScoringFeatures,
    compute_features,
    fit_weights_logistic_regression,
    percentile_95,
    score_edge,
    sigmoid,
)


# --------------------------------------------------------------------------- sigmoid

def test_sigmoid_zero_is_half():
    assert sigmoid(0.0) == pytest.approx(0.5)


def test_sigmoid_bounds_and_monotonic():
    xs = [-50, -5, -1, 0, 1, 5, 50]
    ys = [sigmoid(x) for x in xs]
    assert all(0.0 <= y <= 1.0 for y in ys)
    assert ys == sorted(ys)  # đơn điệu tăng


def test_sigmoid_symmetry():
    for x in (0.3, 1.7, 12.0):
        assert sigmoid(-x) == pytest.approx(1 - sigmoid(x))


def test_sigmoid_no_overflow_on_extreme_values():
    # Không được raise OverflowError dù x rất lớn/rất âm (math.exp(709) sắp overflow)
    assert sigmoid(-1000) == pytest.approx(0.0, abs=1e-9)
    assert sigmoid(1000) == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- percentile_95

def test_percentile_95_matches_numpy_linear_interpolation():
    numpy = pytest.importorskip("numpy")
    values = list(range(1, 101))
    expected = float(numpy.percentile(values, 95))
    assert percentile_95(values) == pytest.approx(expected)


def test_percentile_95_empty_returns_default():
    assert percentile_95([]) == 1.0


def test_percentile_95_single_value():
    assert percentile_95([42]) == 42.0


# --------------------------------------------------------------------------- compute_features

def _agg(count=100, distinct_days=5, last_seen_days_ago=0.5, protocols=("GRPC",),
         class_counts=None) -> TrafficAggregate:
    key = IdentityKey("default", "frontend", "frontend-sa", "default", "svc", "GRPC", 3550)
    agg = TrafficAggregate(identity_key=key, destination_service_ref=None)
    agg.count = count
    agg.distinct_days = distinct_days
    agg.last_seen_days_ago = last_seen_days_ago
    agg.distinct_protocols = set(protocols)
    if class_counts:
        for cls, c in class_counts.items():
            agg.class_counts[cls] = c
    else:
        agg.class_counts[TrafficClass.BUSINESS] = count
    return agg


def test_recency_full_when_just_seen():
    cfg = ScoringConfig(window_days=7)
    f = compute_features(_agg(last_seen_days_ago=0.0), expected_max_count=100, config_match_strength=0.0, config=cfg)
    assert f.recency == pytest.approx(1.0)


def test_recency_zero_when_outside_window():
    cfg = ScoringConfig(window_days=7)
    f = compute_features(_agg(last_seen_days_ago=30), expected_max_count=100, config_match_strength=0.0, config=cfg)
    assert f.recency == pytest.approx(0.0)


def test_distinct_days_ratio_clamped_to_one(config):
    f = compute_features(_agg(distinct_days=999), expected_max_count=100, config_match_strength=0.0, config=config)
    assert f.distinct_days_ratio == pytest.approx(1.0)


def test_protocol_diversity_caps_at_one(config):
    f1 = compute_features(_agg(protocols=("GRPC",)), 100, 0.0, config)
    f2 = compute_features(_agg(protocols=("GRPC", "TCP")), 100, 0.0, config)
    f3 = compute_features(_agg(protocols=("GRPC", "TCP", "HTTP")), 100, 0.0, config)
    assert f1.protocol_diversity == pytest.approx(0.5)
    assert f2.protocol_diversity == pytest.approx(1.0)
    assert f3.protocol_diversity == pytest.approx(1.0)


def test_log_volume_zero_when_expected_max_is_zero(config):
    f = compute_features(_agg(count=50), expected_max_count=0, config_match_strength=0.0, config=config)
    assert f.log_volume == 0.0


def test_log_volume_is_one_when_count_equals_expected_max(config):
    f = compute_features(_agg(count=100), expected_max_count=100, config_match_strength=0.0, config=config)
    assert f.log_volume == pytest.approx(1.0)


def test_healthcheck_and_monitoring_ratio(config):
    agg = _agg(count=100, class_counts={
        TrafficClass.BUSINESS: 60, TrafficClass.HEALTHCHECK: 30, TrafficClass.MONITORING: 10,
    })
    f = compute_features(agg, expected_max_count=100, config_match_strength=0.0, config=config)
    assert f.healthcheck_ratio == pytest.approx(0.3)
    assert f.monitoring_ratio == pytest.approx(0.1)


def test_config_match_strength_passthrough(config):
    f = compute_features(_agg(), expected_max_count=100, config_match_strength=0.77, config=config)
    assert f.config_match_strength == pytest.approx(0.77)


# --------------------------------------------------------------------------- score_edge

def test_score_edge_neutral_config_gives_sigmoid_of_bias():
    neutral = ScoringConfig(
        w_recency=0, w_log_volume=0, w_distinct_days_ratio=0, w_protocol_diversity=0,
        w_healthcheck_ratio=0, w_monitoring_ratio=0, w_config_match_strength=0, bias=0.0,
    )
    f = ScoringFeatures(1, 1, 1, 1, 1, 1, 1)
    assert score_edge(f, neutral) == pytest.approx(0.5)


def test_score_edge_healthcheck_weight_is_negative_by_default(config):
    clean = ScoringFeatures(recency=0.9, log_volume=0.9, distinct_days_ratio=0.9,
                             protocol_diversity=0.9, healthcheck_ratio=0.0, monitoring_ratio=0.0,
                             config_match_strength=0.0)
    noisy = ScoringFeatures(recency=0.9, log_volume=0.9, distinct_days_ratio=0.9,
                             protocol_diversity=0.9, healthcheck_ratio=0.9, monitoring_ratio=0.0,
                             config_match_strength=0.0)
    assert score_edge(clean, config) > score_edge(noisy, config)


def test_score_edge_config_match_strength_increases_score(config):
    base = ScoringFeatures(0.5, 0.5, 0.5, 0.5, 0.0, 0.0, config_match_strength=0.0)
    boosted = ScoringFeatures(0.5, 0.5, 0.5, 0.5, 0.0, 0.0, config_match_strength=1.0)
    assert score_edge(boosted, config) > score_edge(base, config)


# --------------------------------------------------------------------------- CONFIG_MATCH_STRENGTH

def test_config_match_strength_covers_all_findings():
    assert set(CONFIG_MATCH_STRENGTH.keys()) == set(ConfigFinding)


def test_config_match_strength_ordering():
    s = CONFIG_MATCH_STRENGTH
    assert s[ConfigFinding.FOUND_STRUCTURAL] == s[ConfigFinding.FOUND_EMBEDDED] == 1.0
    assert s[ConfigFinding.NOT_FOUND] == 0.0
    assert 0.0 < s[ConfigFinding.FOUND_UNRESOLVED] < 1.0


# --------------------------------------------------------------------------- fit_weights_logistic_regression

def test_fit_weights_logistic_regression_on_separable_data():
    pytest.importorskip("sklearn")
    real = [ScoringFeatures(0.9, 0.9, 0.9, 0.9, 0.0, 0.0, 0.9) for _ in range(20)]
    noise = [ScoringFeatures(0.1, 0.1, 0.1, 0.1, 0.9, 0.9, 0.1) for _ in range(20)]
    features = real + noise
    labels = [1] * 20 + [0] * 20
    new_config, report = fit_weights_logistic_regression(features, labels, k_folds=5)
    assert isinstance(new_config, ScoringConfig)
    assert report["cv_accuracy_mean"] > 0.9
    assert report["cv_recall_mean"] > 0.9
