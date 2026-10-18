"""cluster_context.py — suy port từ Service, path probe, danh tính monitoring, ServiceEntry
(TIER1_DESIGN.md mục 2.1b, 2.2, 8 — chốt 15/10/2026)."""

from tier1.cluster_context import annotate_series, build_cluster_context
from tier1.config_collector.k8s_types import (
    ContainerSpec,
    FakeK8sReader,
    PodInfo,
    ProbeInfo,
    ServiceEntryInfo,
    ServiceInfo,
    ServicePortInfo,
)
from tier1.schema import ServiceRef

from .conftest import make_series

NS = "shared-eval"


def _reader() -> FakeK8sReader:
    pods = [
        PodInfo(name="frontend", namespace=NS, service_account_name="frontend", labels={"app": "frontend"},
                containers=[ContainerSpec(name="server", probes=[
                    ProbeInfo(kind="readiness", http_path="/_healthz", port=8080),
                    ProbeInfo(kind="liveness", http_path="/_healthz?x=1", port=8080),
                ])]),
        PodInfo(name="cartservice", namespace=NS, service_account_name="cartservice", labels={"app": "cartservice"},
                containers=[ContainerSpec(name="server", probes=[ProbeInfo(kind="readiness", is_grpc=True, grpc_service="")])]),
        PodInfo(name="prometheus", namespace="istio-system", service_account_name="prometheus",
                labels={"app.kubernetes.io/name": "prometheus"}),
        PodInfo(name="vmagent", namespace="monitoring", service_account_name="vmagent-sa", labels={"app": "vmagent"}),
        PodInfo(name="custom-scraper", namespace="obs", service_account_name="scraper", labels={"app": "scraper"}),
    ]
    services = [
        ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"},
                    ports=[ServicePortInfo(port=7070, target_port=7070, name="grpc")]),
        ServiceInfo(name="frontend", namespace=NS, selector={"app": "frontend"},
                    ports=[ServicePortInfo(port=80, target_port=8080, name="http")]),
        ServiceInfo(name="multi", namespace=NS, selector={"app": "multi"},
                    ports=[ServicePortInfo(port=80, target_port=8080, name="http-web"),
                           ServicePortInfo(port=9090, target_port=9090, name="grpc-admin")]),
        ServiceInfo(name="ambiguous", namespace=NS, selector={"app": "ambiguous"},
                    ports=[ServicePortInfo(port=80, target_port=8080, name="web"),
                           ServicePortInfo(port=9090, target_port=9090, name="admin")]),
        ServiceInfo(name="scraper", namespace="obs", selector={"app": "scraper"}, ports=[]),
    ]
    return FakeK8sReader(pods=pods, services=services,
                         serviceentries=[ServiceEntryInfo(name="stripe", namespace=NS, hosts=["*.stripe.com"])])


def test_service_ports_probes_monitoring_and_serviceentry_are_indexed():
    ctx = build_cluster_context(_reader(), monitoring_services=("obs/scraper",))
    assert ctx.probe_paths[(NS, "frontend")] == {"/_healthz"}  # 2 probe chuẩn hóa về 1 path
    # tự phát hiện: label prometheus/vmagent + Service khai báo
    assert ctx.monitoring_identities == {
        ("istio-system", "prometheus"), ("monitoring", "vmagent-sa"), ("obs", "scraper"),
    }
    assert ctx.matches_serviceentry("api.stripe.com")
    assert not ctx.matches_serviceentry("stripe.com.evil.io")
    assert ctx.workload_canonical[(NS, "frontend")] == "frontend"


def test_port_inferred_from_single_port_service():
    ctx = build_cluster_context(_reader())
    s = make_series(dest="cartservice", dest_ns=NS, port=None, protocol="GRPC",
                    destination_service_ref=ServiceRef("cartservice", NS))
    annotate_series([s], ctx)
    assert (s.port, s.port_source) == (7070, "inferred_single_port")


def test_port_inferred_from_targetport_not_service_port():
    ctx = build_cluster_context(_reader())
    s = make_series(dest="frontend", dest_ns=NS, port=None, protocol="HTTP",
                    destination_service_ref=ServiceRef("frontend", NS))
    annotate_series([s], ctx)
    assert s.port == 8080  # targetPort (port pod thật), không phải port 80 của Service


def test_port_inferred_by_protocol_match_on_multi_port_service():
    ctx = build_cluster_context(_reader())
    s = make_series(dest="multi", dest_ns=NS, port=None, protocol="GRPC",
                    destination_service_ref=ServiceRef("multi", NS))
    annotate_series([s], ctx)
    assert (s.port, s.port_source) == (9090, "inferred_protocol_match")


def test_port_unknown_when_ambiguous_never_guessed():
    ctx = build_cluster_context(_reader())
    s = make_series(dest="ambiguous", dest_ns=NS, port=None, protocol="HTTP",
                    destination_service_ref=ServiceRef("ambiguous", NS))
    annotate_series([s], ctx)
    assert (s.port, s.port_source) == (None, "unknown")


def test_observed_port_kept():
    ctx = build_cluster_context(_reader())
    s = make_series(dest="cartservice", dest_ns=NS, port=7000, port_source="unknown",
                    destination_service_ref=ServiceRef("cartservice", NS))
    annotate_series([s], ctx)
    assert (s.port, s.port_source) == (7000, "observed")


def test_probe_path_flag_only_for_the_destination_that_declares_it():
    ctx = build_cluster_context(_reader())
    to_frontend = make_series(dest="frontend", dest_ns=NS, path="/_healthz?probe=1")
    to_cart = make_series(dest="cartservice", dest_ns=NS, path="/_healthz")
    annotate_series([to_frontend, to_cart], ctx)
    assert "probe_path" in to_frontend.flags
    assert "probe_path" not in to_cart.flags


def test_verified_monitoring_flag_by_namespace_and_service_account():
    ctx = build_cluster_context(_reader())
    real = make_series(source="prometheus", source_workload_namespace="istio-system", path="/metrics")
    real.source_service_account = "prometheus"
    fake = make_series(source="prometheus", source_workload_namespace=NS, path="/metrics")
    fake.source_service_account = "default"
    annotate_series([real, fake], ctx)
    assert "verified_monitoring" in real.flags
    assert "verified_monitoring" not in fake.flags


def test_serviceentry_flag_from_destination_host():
    ctx = build_cluster_context(_reader())
    s = make_series(dest="unknown", path=None, protocol="TCP")
    s.destination_service_host = "api.stripe.com"
    annotate_series([s], ctx)
    assert "serviceentry_host" in s.flags


def test_annotate_without_context_only_normalizes_port_source():
    s = make_series(port=1234, port_source="unknown")
    annotate_series([s], None)
    assert s.port_source == "observed"
