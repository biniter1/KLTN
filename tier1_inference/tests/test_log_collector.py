"""log_collector.py — access log ztunnel từ VictoriaLogs (stack LOG_METRIC), gộp với metrics."""

from datetime import datetime, timezone

from tier1.log_collector import (
    FakeLogsClient,
    build_ztunnel_flow_query,
    collect_ztunnel_flows,
    merge_log_series,
)
from tier1.traffic_collector import FakePrometheusClient, collect_traffic

from .conftest import make_series

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
NS = "shared-eval"


def _row(src="checkoutservice", dst="redis-cart", port=6379, day="2026-10-07", hits=10, **over):
    row = {
        "_time": f"{day}T00:00:00Z",
        "src.namespace": NS, "src.workload": src,
        "src.identity": f"spiffe://cluster.local/ns/{NS}/sa/{src}",
        "dst.namespace": NS, "dst.workload": dst,
        "dst.service": f"{dst}.{NS}.svc.cluster.local",
        "dst.identity": f"spiffe://cluster.local/ns/{NS}/sa/{dst}",
        "dst.hbone_addr": f"10.244.1.5:{port}",
        "dst.addr": "10.244.1.5:15008",
        "hits": str(hits),
    }
    row.update(over)
    return row


def test_query_uses_inbound_only_daily_stats_and_namespace_filter():
    q = build_ztunnel_flow_query(datetime(2026, 10, 2, tzinfo=timezone.utc), datetime(2026, 10, 9, tzinfo=timezone.utc), [NS])
    assert q.startswith("_time:[2026-10-02T00:00:00Z, 2026-10-09T00:00:00Z)")
    assert '"connection complete"' in q and "| unpack_logfmt" in q
    assert 'direction:="inbound"' in q  # mỗi kết nối 2 ztunnel ghi -> chỉ đếm 1 lần
    assert f'dst.namespace:="{NS}"' in q
    assert "stats by (_time:1d," in q and "dst.hbone_addr" in q


def test_flows_become_tcp_series_with_observed_port_and_days():
    client = FakeLogsClient([_row(day="2026-10-06", hits=4), _row(day="2026-10-07", hits=6)])
    res = collect_ztunnel_flows(client, window_days=7, namespaces=[NS], now=NOW)
    assert len(res.series) == 1
    s = res.series[0]
    assert (s.protocol, s.port, s.port_source, s.evidence_source) == ("TCP", 6379, "observed_log", "logs")
    assert s.count == 10 and s.traffic_days == ["2026-10-06", "2026-10-07"] and s.distinct_days == 2
    assert s.last_seen_days_ago == 1.0
    assert s.source_service_account == "checkoutservice"
    assert (s.destination_service_ref.service_name, s.destination_service_ref.service_namespace) == ("redis-cart", NS)
    assert res.daily_counts == [{"2026-10-06": 4, "2026-10-07": 6}]


def test_port_from_dst_addr_when_no_hbone_and_hbone_port_ignored():
    client = FakeLogsClient([
        _row(**{"dst.hbone_addr": "", "dst.addr": "10.0.0.9:5432"}, dst="report-db", port=0),
        _row(**{"dst.hbone_addr": "", "dst.addr": "10.0.0.9:15008"}, dst="x"),  # chỉ thấy cổng HBONE -> bỏ
    ])
    res = collect_ztunnel_flows(client, now=NOW)
    assert [s.port for s in res.series] == [5432]
    assert res.rows_skipped == 1


def test_workload_mapped_to_canonical_service():
    """Log ghi tên WORKLOAD (harness-...), metric ghi canonical -> map qua cluster context."""
    client = FakeLogsClient([_row(src="harness-strong-hidden-paymentservice-redis-cart")])
    res = collect_ztunnel_flows(client, now=NOW,
                                workload_canonical={(NS, "harness-strong-hidden-paymentservice-redis-cart"): "paymentservice"})
    assert res.series[0].source_canonical_service == "paymentservice"


def test_merge_sets_port_on_metric_series_and_drops_duplicate_log_edge():
    metric = make_series(source="frontend", dest="cartservice", dest_ns=NS, source_ns=NS,
                         source_workload_namespace=NS, port=None, protocol="GRPC")
    metric.source_service_account = "frontend"
    log_same = collect_ztunnel_flows(FakeLogsClient([_row(src="frontend", dst="cartservice", port=7070)]), now=NOW)
    kept, _ = merge_log_series([metric], log_same.series, log_same.daily_counts)
    assert (metric.port, metric.port_source) == (7070, "observed_log")
    assert kept == []  # cùng kết nối, đã đếm theo request ở metric


def test_merge_keeps_tcp_edge_seen_only_in_logs():
    metric = make_series(source="frontend", dest="cartservice", source_ns=NS, dest_ns=NS, source_workload_namespace=NS)
    logs = collect_ztunnel_flows(FakeLogsClient([_row()]), now=NOW)
    kept, kept_daily = merge_log_series([metric], logs.series, logs.daily_counts)
    assert [(s.source_canonical_service, s.destination_canonical_service) for s in kept] == [("checkoutservice", "redis-cart")]
    assert len(kept_daily) == 1


def test_merge_multiple_ports_leaves_port_unknown_with_flag():
    metric = make_series(source="checkoutservice", dest="redis-cart", source_ns=NS, dest_ns=NS,
                         source_workload_namespace=NS, port=None, protocol="TCP", path=None)
    metric.source_service_account = "checkoutservice"
    logs = collect_ztunnel_flows(FakeLogsClient([_row(port=6379), _row(port=26379)]), now=NOW)
    merge_log_series([metric], logs.series, logs.daily_counts)
    assert metric.port is None and "multiple_ports_seen" in metric.flags


def test_collect_traffic_with_log_client_adds_log_only_edges():
    prom = FakePrometheusClient(matcher=lambda q, t: [])
    logs = FakeLogsClient([_row()])
    res = collect_traffic(prom, window_days=7, namespaces=[NS], now=NOW, check_gaps=False, log_client=logs)
    assert res.log_series_added == 1 and res.log_rows_read == 1
    assert res.series[0].evidence_source == "logs"
    assert len(res.daily_counts) == len(res.series)
