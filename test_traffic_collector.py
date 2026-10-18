"""traffic_collector.py — TRAFFIC_COLLECTION_PIPELINE.md mục 1-6 (FakePrometheusClient,
không cần Prometheus/cluster thật)."""

from __future__ import annotations

from datetime import datetime, timezone

from tier1.traffic_collector import (
    FakePrometheusClient,
    _Accumulator,
    _extract_service_account,
    _to_traffic_series,
    build_daily_bucket_query,
    build_error_query,
    build_http_grpc_query,
    build_scrape_gap_query,
    build_tcp_query,
    check_scrape_gaps,
    collect_traffic,
    parse_daily_result,
    parse_window_result,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def _metric(**overrides) -> dict:
    base = {
        "source_workload_namespace": "default",
        "source_canonical_service": "frontend",
        "source_principal": "spiffe://cluster.local/ns/default/sa/frontend-sa",
        "destination_workload_namespace": "default",
        "destination_canonical_service": "productcatalogservice",
        "destination_principal": "spiffe://cluster.local/ns/default/sa/productcatalog-sa",
        "destination_service_name": "productcatalogservice",
        "destination_service_namespace": "default",
        "destination_port": "3550",
        "request_protocol": "grpc",
        "request_path": "/hipstershop.ProductCatalogService/ListProducts",
    }
    base.update(overrides)
    return base


def _result(value: str, **metric_overrides) -> dict:
    return {"metric": _metric(**metric_overrides), "value": [1695600000, value]}


# ---------------------------------------------------------------------------
# PromQL builders
# ---------------------------------------------------------------------------


def test_build_http_grpc_query_default():
    q = build_http_grpc_query(7)
    assert q == 'increase(istio_requests_total{reporter="destination"}[7d])'


def test_build_http_grpc_query_with_namespace_filter():
    q = build_http_grpc_query(7, namespaces=["default", "checkout"])
    assert 'destination_workload_namespace=~"default|checkout"' in q


def test_build_http_grpc_query_injected_true():
    q = build_http_grpc_query(7, injected=True)
    assert 'test_harness="true"' in q


def test_build_http_grpc_query_injected_false():
    q = build_http_grpc_query(7, injected=False)
    assert 'test_harness!="true"' in q


def test_build_tcp_query_uses_connections_opened_metric():
    q = build_tcp_query(7)
    assert q.startswith("increase(istio_tcp_connections_opened_total{")


def test_build_error_query_filters_5xx():
    q = build_error_query(7)
    assert 'response_code=~"5.."' in q
    assert q.startswith("increase(istio_requests_total{")


def test_build_scrape_gap_query():
    assert build_scrape_gap_query() == 'up{job="istio-proxy"} == 0'


def test_build_daily_bucket_query_uses_at_modifier():
    q, day_iso = build_daily_bucket_query(0, NOW)
    assert "@ " in q
    assert q.startswith("increase(istio_requests_total{")
    assert "[1d] @" in q


# ---------------------------------------------------------------------------
# check_scrape_gaps
# ---------------------------------------------------------------------------


def test_check_scrape_gaps_none():
    client = FakePrometheusClient(responses={build_scrape_gap_query(): []})
    report = check_scrape_gaps(client)
    assert report.has_gap is False


def test_check_scrape_gaps_detects_gap():
    client = FakePrometheusClient(
        responses={build_scrape_gap_query(): [{"metric": {"instance": "10.0.0.5:15090"}, "value": [1, "0"]}]}
    )
    report = check_scrape_gaps(client)
    assert report.has_gap is True
    assert report.affected_targets == ["10.0.0.5:15090"]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def test_extract_service_account_from_spiffe():
    assert _extract_service_account("spiffe://cluster.local/ns/default/sa/frontend-sa") == "frontend-sa"


def test_extract_service_account_none_when_missing():
    assert _extract_service_account(None) is None
    assert _extract_service_account("not-a-spiffe-uri") is None


def test_parse_window_result_accumulates_count():
    accumulators: dict = {}
    parse_window_result([_result("1200")], accumulators, "count")
    assert len(accumulators) == 1
    acc = next(iter(accumulators.values()))
    assert acc.count == 1200.0


def test_parse_daily_result_marks_distinct_day():
    accumulators: dict = {}
    parse_window_result([_result("50")], accumulators, "count")  # xác lập series tồn tại trước
    parse_daily_result([_result("50")], accumulators, "2026-09-20", day_offset=0)
    acc = next(iter(accumulators.values()))
    assert "2026-09-20" in acc.days_with_traffic
    assert acc.last_seen_days_ago == 0.0


def test_parse_daily_result_zero_value_not_counted():
    accumulators: dict = {}
    parse_window_result([_result("50")], accumulators, "count")
    parse_daily_result([_result("0")], accumulators, "2026-09-20", day_offset=0)
    acc = next(iter(accumulators.values()))
    assert acc.days_with_traffic == set()


def test_parse_daily_result_ignores_series_not_in_window():
    """Series chỉ xuất hiện ở query theo ngày, chưa từng có trong query window total ->
    không tạo accumulator "ma" (query window total mới là nguồn xác nhận tồn tại)."""
    accumulators: dict = {}
    parse_daily_result([_result("50")], accumulators, "2026-09-20", day_offset=0)
    assert accumulators == {}


def test_to_traffic_series_maps_fields():
    acc = _Accumulator(metric=_metric(test_harness="true", injection_id="inj_1"))
    acc.count = 1200
    acc.error_count = 5
    acc.days_with_traffic = {"d1", "d2"}
    acc.last_seen_days_ago = 0.1
    series = _to_traffic_series(acc)

    assert series.source_canonical_service == "frontend"
    assert series.source_service_account == "frontend-sa"
    assert series.destination_canonical_service == "productcatalogservice"
    assert series.destination_service_account == "productcatalog-sa"
    assert series.destination_service_ref.service_name == "productcatalogservice"
    assert series.protocol == "GRPC"
    assert series.port == 3550
    assert series.path == "/hipstershop.ProductCatalogService/ListProducts"
    assert series.count == 1200
    assert series.distinct_days == 2
    assert series.error_count == 5
    assert series.is_injected is True
    assert series.injection_id == "inj_1"


def test_to_traffic_series_defaults_when_labels_missing():
    acc = _Accumulator(metric={})
    series = _to_traffic_series(acc)
    assert series.is_injected is False
    assert series.injection_id is None
    assert series.destination_service_ref is None
    assert series.port == 0


# ---------------------------------------------------------------------------
# Integration — collect_traffic end-to-end
# ---------------------------------------------------------------------------


def test_collect_traffic_end_to_end():
    window_days = 7
    responses: dict[str, list[dict]] = {
        build_scrape_gap_query(): [],
        build_http_grpc_query(window_days): [_result("1200")],
        build_error_query(window_days): [_result("5")],
        build_tcp_query(window_days): [],
    }
    for day_offset in range(window_days):
        q, day_iso = build_daily_bucket_query(day_offset, NOW)
        # traffic mỗi ngày trong cửa sổ (distinct_days=7), trừ ngày offset=3 (thưa)
        responses[q] = [] if day_offset == 3 else [_result("100")]

    client = FakePrometheusClient(responses=responses)
    result = collect_traffic(client, window_days=window_days, now=NOW)

    assert result.scrape_gap.has_gap is False
    assert len(result.series) == 1
    s = result.series[0]
    assert s.count == 1200
    assert s.error_count == 5
    assert s.distinct_days == 6  # 7 ngày - 1 ngày thưa (offset 3)
    assert s.last_seen_days_ago == 0.0


def test_collect_traffic_detects_scrape_gap():
    client = FakePrometheusClient(
        responses={build_scrape_gap_query(): [{"metric": {"instance": "x"}, "value": [1, "0"]}]},
        matcher=lambda promql, time: [],
    )
    result = collect_traffic(client, window_days=1, now=NOW)
    assert result.scrape_gap.has_gap is True


def test_collect_traffic_includes_tcp_series():
    window_days = 1
    tcp_metric = _result("300", request_protocol="tcp", destination_canonical_service="redis-cart", destination_port="6379")
    responses = {
        build_scrape_gap_query(): [],
        build_http_grpc_query(window_days): [],
        build_error_query(window_days): [],
        build_tcp_query(window_days): [tcp_metric],
    }
    for day_offset in range(window_days):
        q, _ = build_daily_bucket_query(day_offset, NOW)
        responses[q] = []

    client = FakePrometheusClient(responses=responses)
    result = collect_traffic(client, window_days=window_days, now=NOW)
    assert len(result.series) == 1
    assert result.series[0].protocol == "TCP"
    assert result.series[0].destination_canonical_service == "redis-cart"


def test_collect_traffic_filters_zero_count_series():
    window_days = 1
    responses = {
        build_scrape_gap_query(): [],
        build_http_grpc_query(window_days): [_result("0")],
        build_error_query(window_days): [],
        build_tcp_query(window_days): [],
    }
    for day_offset in range(window_days):
        q, _ = build_daily_bucket_query(day_offset, NOW)
        responses[q] = []

    client = FakePrometheusClient(responses=responses)
    result = collect_traffic(client, window_days=window_days, now=NOW)
    assert result.series == []


def test_collect_traffic_injected_only_filter_builds_correct_query():
    window_days = 1
    q_http = build_http_grpc_query(window_days, injected=True)
    responses = {
        build_scrape_gap_query(): [],
        q_http: [_result("42", test_harness="true", injection_id="run_003")],
        build_error_query(window_days, injected=True): [],
        build_tcp_query(window_days, injected=True): [],
    }
    for day_offset in range(window_days):
        q, _ = build_daily_bucket_query(day_offset, NOW, injected=True)
        responses[q] = []

    client = FakePrometheusClient(responses=responses)
    result = collect_traffic(client, window_days=window_days, injected=True, now=NOW)
    assert len(result.series) == 1
    assert result.series[0].is_injected is True
    assert result.series[0].injection_id == "run_003"
