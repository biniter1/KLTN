"""classify_series() — TIER1_DESIGN.md mục 2.

Thứ tự ưu tiên rule PHẢI đúng: DNS > CONTROL_PLANE > HEALTHCHECK > MONITORING > BUSINESS > UNKNOWN.
Mỗi test dưới đây cố tình đặt nhiều tín hiệu "xung đột" cùng lúc để khẳng định đúng
rule nào thắng, không chỉ test rule đơn lẻ.
"""

from tier1.classification import classify_all, classify_series
from tier1.schema import TrafficClass

from .conftest import make_series


def test_dns_by_port_wins_over_everything(config):
    s = make_series(port=53, protocol="TCP", path="/GetProduct", source_workload_namespace="istio-system")
    assert classify_series(s, config) == TrafficClass.DNS


def test_dns_by_protocol_name(config):
    s = make_series(protocol="DNS", port=9999, path=None)
    assert classify_series(s, config) == TrafficClass.DNS


def test_control_plane_wins_over_healthcheck_path(config):
    s = make_series(
        source_workload_namespace="istio-system",
        path="/GetProduct",  # path nghiệp vụ bình thường, không phải healthcheck
    )
    assert classify_series(s, config) == TrafficClass.CONTROL_PLANE


def test_control_plane_kube_system(config):
    s = make_series(source_workload_namespace="kube-system")
    assert classify_series(s, config) == TrafficClass.CONTROL_PLANE


def test_healthcheck_path_prefixes(config):
    for path in ("/health", "/healthz", "/ready", "/readyz", "/live", "/livez", "/metrics"):
        s = make_series(path=path, source="prometheus")  # source cũng khớp MONITORING, nhưng path thắng
        assert classify_series(s, config) == TrafficClass.HEALTHCHECK, path


def test_healthcheck_case_insensitive_and_prefix_match(config):
    s = make_series(path="/HealthCheck/Deep")
    assert classify_series(s, config) == TrafficClass.HEALTHCHECK


def test_monitoring_by_known_source(config):
    s = make_series(source="prometheus", path="/")  # path "/" không khớp healthcheck prefix nào
    assert classify_series(s, config) == TrafficClass.MONITORING


def test_monitoring_source_case_insensitive(config):
    s = make_series(source="Blackbox-Exporter", path="/")
    assert classify_series(s, config) == TrafficClass.MONITORING


def test_business_default_for_recognized_protocol(config):
    for proto in ("HTTP", "GRPC", "TCP"):
        s = make_series(protocol=proto, path="/GetCart")
        assert classify_series(s, config) == TrafficClass.BUSINESS, proto


def test_unknown_for_unrecognized_protocol(config):
    s = make_series(protocol="CUSTOM-RPC", path=None)
    assert classify_series(s, config) == TrafficClass.UNKNOWN


def test_classify_all_mutates_in_place_and_returns_same_list(config):
    series = [make_series(path="/health"), make_series(protocol="CUSTOM", path=None)]
    result = classify_all(series, config)
    assert result is series
    assert series[0].traffic_class == TrafficClass.HEALTHCHECK
    assert series[1].traffic_class == TrafficClass.UNKNOWN
