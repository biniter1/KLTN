"""aggregate_series() — gộp TrafficSeries theo identity_key, chuẩn bị input cho scoring."""

import pytest

from tier1.aggregation import aggregate_series
from tier1.classification import classify_all
from tier1.schema import TrafficClass

from .conftest import make_series


def test_raises_if_not_classified():
    s = make_series()  # traffic_class chưa gán (None mặc định)
    with pytest.raises(ValueError):
        aggregate_series([s])


def test_groups_multiple_series_into_one_edge(config):
    a = make_series(path="/Business/A", count=100, distinct_days=3, last_seen_days_ago=1.0,
                     error_count=2, protocol="GRPC")
    b = make_series(path="/grpc.health.v1.Health/Check", count=10, distinct_days=7,
                     last_seen_days_ago=0.1, error_count=0, protocol="GRPC")
    classify_all([a, b], config)
    aggs = aggregate_series([a, b])

    assert len(aggs) == 1
    agg = next(iter(aggs.values()))
    assert agg.count == 110
    assert agg.error_count == 2
    assert agg.distinct_days == 7           # max(3, 7)
    assert agg.last_seen_days_ago == 0.1     # min(1.0, 0.1)
    assert agg.distinct_protocols == {"GRPC"}
    assert agg.class_counts[TrafficClass.BUSINESS] == 100
    assert agg.class_counts[TrafficClass.HEALTHCHECK] == 10


def test_different_identity_key_creates_separate_edges(config):
    a = make_series(dest="serviceA", count=10)
    b = make_series(dest="serviceB", count=20)
    classify_all([a, b], config)
    aggs = aggregate_series([a, b])
    assert len(aggs) == 2


def test_error_ratio_and_class_ratio(config):
    s = make_series(count=100, error_count=25, path="/Business")
    classify_all([s], config)
    agg = next(iter(aggregate_series([s]).values()))
    assert agg.error_ratio == pytest.approx(0.25)
    assert agg.class_ratio(TrafficClass.BUSINESS) == pytest.approx(1.0)
    assert agg.class_ratio(TrafficClass.HEALTHCHECK) == pytest.approx(0.0)


def test_dominant_class_business_wins_when_tied_or_ahead(config):
    business = make_series(path="/Business", count=50)
    healthcheck = make_series(path="/health", count=50)  # đúng bằng nhau
    classify_all([business, healthcheck], config)
    agg = next(iter(aggregate_series([business, healthcheck]).values()))
    assert agg.dominant_traffic_class() == TrafficClass.BUSINESS


def test_dominant_class_falls_back_when_business_is_minority(config):
    business = make_series(path="/Business", count=10)
    healthcheck = make_series(path="/health", count=90)
    classify_all([business, healthcheck], config)
    agg = next(iter(aggregate_series([business, healthcheck]).values()))
    assert agg.dominant_traffic_class() == TrafficClass.HEALTHCHECK


def test_dominant_class_no_business_at_all(config):
    # Cùng identity_key (cùng source/dest/protocol/port) nhưng 2 series khác path, nên có
    # thể mang traffic_class khác nhau thật (mục 2: phân loại chạy Ở CẤP SERIES). Gán thẳng
    # traffic_class để cô lập test khỏi chi tiết rule nhận diện MONITORING/HEALTHCHECK.
    healthcheck = make_series(path="/health", count=30)
    monitoring = make_series(path="/probe", count=70)
    healthcheck.traffic_class = TrafficClass.HEALTHCHECK
    monitoring.traffic_class = TrafficClass.MONITORING
    agg = next(iter(aggregate_series([healthcheck, monitoring]).values()))
    assert agg.dominant_traffic_class() == TrafficClass.MONITORING
