"""classify_series() — TIER1_DESIGN.md mục 2.2 (thứ tự chốt 15/10/2026).

Thứ tự: DNS > MONITORING (đã xác minh) > CONTROL_PLANE > HEALTHCHECK > BUSINESS > UNKNOWN.
Mỗi test đặt nhiều tín hiệu "xung đột" cùng lúc để khẳng định đúng luật nào thắng.
"""

from tier1.classification import classify_all, classify_series
from tier1.config import ScoringConfig
from tier1.schema import ServiceRef, TrafficClass

from .conftest import make_series


# --------------------------------------------------------------------------- DNS

def test_dns_by_port_wins_over_everything(config):
    s = make_series(port=53, protocol="TCP", path="/GetProduct", source_workload_namespace="istio-system")
    assert classify_series(s, config) == TrafficClass.DNS


def test_dns_by_protocol_name(config):
    s = make_series(protocol="DNS", port=9999, path=None)
    assert classify_series(s, config) == TrafficClass.DNS


def test_dns_by_destination_service_even_without_port(config):
    s = make_series(dest="coredns", port=None, protocol="TCP", path=None,
                    destination_service_ref=ServiceRef("kube-dns", "kube-system"))
    assert classify_series(s, config) == TrafficClass.DNS


# --------------------------------------------------------------------------- MONITORING

def test_verified_monitoring_flag_from_cluster_wins_over_control_plane_and_healthcheck(config):
    """Prometheus thật: ở istio-system, gọi /metrics — bản cũ ra CONTROL_PLANE."""
    s = make_series(source="prometheus", path="/metrics", source_workload_namespace="istio-system")
    s.add_flag("verified_monitoring")
    assert classify_series(s, config) == TrafficClass.MONITORING


def test_verified_monitoring_by_configured_identity_experiment_config():
    """Phương án (a): harness thí nghiệm chạy SA `prometheus`/`grafana`/`blackbox-exporter`
    -> EXPERIMENT_CONFIG khai "*/<tên>"; config mặc định KHÔNG tin (mặc định chặt)."""
    from tier1.config import DEFAULT_CONFIG, EXPERIMENT_CONFIG

    for name in ("prometheus", "grafana", "blackbox-exporter"):
        s = make_series(source=name, path="/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
                        source_workload_namespace="shared-eval")
        s.source_service_account = name
        assert classify_series(s, EXPERIMENT_CONFIG) == TrafficClass.MONITORING, name
        s2 = make_series(source=name, path="/x", source_workload_namespace="shared-eval")
        s2.source_service_account = name
        assert classify_series(s2, DEFAULT_CONFIG) == TrafficClass.BUSINESS, name


def test_claimed_monitoring_name_without_verified_identity_is_not_monitoring(config):
    """Chống giả tên: canonical `prometheus` nhưng SA `default` -> BUSINESS + cờ."""
    s = make_series(source="prometheus", path="/hipstershop.PaymentService/Charge")
    s.source_service_account = "default"
    assert classify_series(s, config) == TrafficClass.BUSINESS
    assert "claimed_monitoring" in s.flags


def test_unknown_principal_never_verified(config):
    """Nguồn ngoài mesh -> không có SPIFFE principal -> không xác minh được."""
    s = make_series(source="prometheus", path="/hipstershop.X/Y")
    s.source_service_account = "unknown"
    cfg = ScoringConfig(verified_monitoring_identities=("*",))
    assert classify_series(s, cfg) == TrafficClass.BUSINESS


# --------------------------------------------------------------------------- CONTROL_PLANE

def test_control_plane_by_source_namespace(config):
    s = make_series(source_workload_namespace="kube-system", path="/GetProduct")
    assert classify_series(s, config) == TrafficClass.CONTROL_PLANE


def test_control_plane_by_destination_namespace(config):
    s = make_series(dest_ns="istio-system", path="/GetProduct")
    assert classify_series(s, config) == TrafficClass.CONTROL_PLANE


# --------------------------------------------------------------------------- HEALTHCHECK

def test_healthcheck_default_last_segments(config):
    for path in ("/health", "/healthz", "/readyz", "/livez", "/ready", "/live", "/metrics", "/api/v1/healthz"):
        assert classify_series(make_series(path=path), config) == TrafficClass.HEALTHCHECK, path


def test_healthcheck_normalizes_case_query_and_trailing_slash(config):
    assert classify_series(make_series(path="/HEALTHZ/?probe=1"), config) == TrafficClass.HEALTHCHECK


def test_healthcheck_grpc_health(config):
    for path in ("/grpc.health.v1.Health/Check", "/grpc.health.v1.Health/Watch"):
        assert classify_series(make_series(path=path), config) == TrafficClass.HEALTHCHECK, path


def test_grpc_reflection_is_business_by_default(config):
    """Harness thí nghiệm sinh traffic dependency cấy qua reflection (grpcurl) -> mặc định
    KHÔNG coi là healthcheck; muốn coi là nhiễu thì khai trong healthcheck_extra_paths."""
    path = "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo"
    assert classify_series(make_series(path=path), config) == TrafficClass.BUSINESS
    cfg = ScoringConfig(healthcheck_extra_paths=(path,))
    assert classify_series(make_series(path=path), cfg) == TrafficClass.HEALTHCHECK


def test_healthcheck_does_not_match_by_prefix(config):
    """So theo segment, không theo tiền tố — bản cũ bắt nhầm các path này."""
    for path in ("/livestream", "/healthcheck-report/export", "/readyness-survey", "/cart/123/status"):
        assert classify_series(make_series(path=path), config) == TrafficClass.BUSINESS, path


def test_healthcheck_from_probe_path_flag(config):
    """Tầng 1: path health khai trong probe của service đích (collector gắn cờ)."""
    s = make_series(path="/_healthz")
    assert classify_series(s, config) == TrafficClass.BUSINESS
    s.add_flag("probe_path")
    assert classify_series(s, config) == TrafficClass.HEALTHCHECK


def test_healthcheck_extra_paths_from_operations_team():
    cfg = ScoringConfig(healthcheck_extra_paths=("/internal/ping",))
    assert classify_series(make_series(path="/internal/ping"), cfg) == TrafficClass.HEALTHCHECK


# --------------------------------------------------------------------------- BUSINESS / UNKNOWN

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
