"""
Ngữ cảnh cluster cho Tầng 1 (TIER1_DESIGN.md mục 2.1, 2.2, 8 — chốt 15/10/2026).

Traffic từ Prometheus/VictoriaMetrics thiếu vài thông tin mà chỉ cluster mới có:
  - port của cạnh (nhãn `destination_port` vắng hẳn trong `istio_requests_total`)
    -> suy từ Service spec qua `destination_service_ref` (mục 2.1b);
  - path health THẬT của từng service -> đọc từ probe trong pod spec (mục 2.2, tầng 1
    của HEALTHCHECK);
  - danh tính (namespace, service account) của công cụ monitoring THẬT -> tự phát hiện
    Prometheus/vmagent (mục 2.2, luật MONITORING đã xác minh);
  - host của ServiceEntry -> cạnh ra ngoài cluster đã khai báo (mục 8, scope).

Module này đọc cluster MỘT LẦN (`build_cluster_context`, dùng lại `K8sReader` của Config
Collector — cùng quyền chỉ đọc) rồi `annotate_series` gắn port/cờ vào series NGAY LÚC THU
THẬP. Nhờ vậy file JSON trial (08_export) tự chứa đủ thông tin, và đường đánh giá offline
(09_run_experiment, không có cluster) vẫn phân loại đúng — phân loại ở `classification.py`
chỉ đọc cờ, không gọi cluster.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .schema import TrafficSeries, normalize_path

# Tiền tố tên port / appProtocol (quy ước Istio "protocol selection") ứng với protocol
# của traffic — dùng khi Service có nhiều port (mục 2.1b, inferred_protocol_match).
_PORT_PROTOCOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "GRPC": ("grpc", "http2", "h2c"),
    "HTTP": ("http", "http2", "h2c"),
    "TCP": ("tcp", "redis", "mongo", "mysql", "postgres", "tls", "kafka"),
}

# Nhãn pod nhận diện công cụ monitoring (bổ sung cho Service khai ở `monitoring_services`).
_MONITORING_APP_LABELS = ("prometheus", "vmagent")


@dataclass(frozen=True)
class PortSpec:
    port: int
    target_port: int
    name: Optional[str] = None
    app_protocol: Optional[str] = None


@dataclass
class ClusterContext:
    # (namespace, service_name) -> các port của Service
    service_ports: dict[tuple[str, str], list[PortSpec]] = field(default_factory=dict)
    # (namespace, canonical_service) -> path probe HTTP đã chuẩn hóa
    probe_paths: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    # (namespace, service_account) của công cụ monitoring đã xác minh
    monitoring_identities: set[tuple[str, str]] = field(default_factory=set)
    # host (có thể wildcard "*.example.com") của mọi ServiceEntry
    serviceentry_hosts: tuple[str, ...] = ()
    # (namespace, tên workload) -> canonical_service — dùng cho access log ztunnel
    # (log ghi tên workload, metric ghi canonical)
    workload_canonical: dict[tuple[str, str], str] = field(default_factory=dict)

    def matches_serviceentry(self, host: Optional[str]) -> bool:
        if not host:
            return False
        h = host.lower().rstrip(".")
        return any(fnmatch.fnmatchcase(h, pat.lower()) for pat in self.serviceentry_hosts)


def _selector_matches(selector: Optional[dict], labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def build_cluster_context(
    reader,
    monitoring_services: Iterable[str] = ("istio-system/prometheus",),
) -> ClusterContext:
    """Đọc cluster 1 lần. `reader`: `tier1.config_collector.k8s_types.K8sReader`
    (KubernetesReader thật hoặc FakeK8sReader). `monitoring_services`: "ns/tên-service" của
    Prometheus/vmagent mà hệ thống đang đọc — Service -> selector -> pod -> (ns, SA)."""
    from .config_collector.identity import canonical_service_of, service_account_of

    pods = list(reader.list_pods())
    services = list(reader.list_services())
    serviceentries = list(reader.list_serviceentries())

    ctx = ClusterContext()
    for svc in services:
        ctx.service_ports[(svc.namespace, svc.name)] = [
            PortSpec(port=p.port, target_port=p.target_port or p.port, name=p.name, app_protocol=p.app_protocol)
            for p in svc.ports
        ]

    for pod in pods:
        canonical = canonical_service_of(pod)
        ctx.workload_canonical[(pod.namespace, pod.name)] = canonical
        for c in pod.containers:
            for probe in getattr(c, "probes", []) or []:
                if probe.http_path:
                    ctx.probe_paths.setdefault((pod.namespace, canonical), set()).add(normalize_path(probe.http_path))

    wanted = {tuple(s.split("/", 1)) for s in monitoring_services if "/" in s}
    for svc in services:
        if (svc.namespace, svc.name) not in wanted:
            continue
        for pod in pods:
            if pod.namespace == svc.namespace and _selector_matches(svc.selector, pod.labels):
                ctx.monitoring_identities.add((pod.namespace, service_account_of(pod)))
    for pod in pods:
        labels = pod.labels or {}
        if labels.get("app.kubernetes.io/name") in _MONITORING_APP_LABELS or labels.get("app") in _MONITORING_APP_LABELS:
            ctx.monitoring_identities.add((pod.namespace, service_account_of(pod)))

    ctx.serviceentry_hosts = tuple(h for se in serviceentries for h in se.hosts)
    return ctx


def _port_matches_protocol(spec: PortSpec, protocol: str) -> bool:
    prefixes = _PORT_PROTOCOL_PREFIXES.get(protocol.upper(), ())
    for label in (spec.app_protocol, spec.name):
        if not label:
            continue
        low = label.lower()
        if any(low == p or low.startswith(p + "-") or low.startswith(p + "_") for p in prefixes):
            return True
    return False


def infer_port(series: TrafficSeries, ctx: ClusterContext) -> None:
    """mục 2.1b — chỉ chạy khi `series.port is None`. Không đoán: không chọn được thì để
    None + `port_source = unknown`."""
    if series.port is not None:
        if series.port_source == "unknown":
            series.port_source = "observed"
        return
    ref = series.destination_service_ref
    if ref is None:
        return
    ports = ctx.service_ports.get((ref.service_namespace, ref.service_name), [])
    if len(ports) == 1:
        series.port = ports[0].target_port
        series.port_source = "inferred_single_port"
        return
    matched = [p for p in ports if _port_matches_protocol(p, series.protocol)]
    if len(matched) == 1:
        series.port = matched[0].target_port
        series.port_source = "inferred_protocol_match"


def annotate_series(series_list: list[TrafficSeries], ctx: Optional[ClusterContext]) -> list[TrafficSeries]:
    """Gắn port suy ra + cờ `probe_path` / `verified_monitoring` / `serviceentry_host`.
    Sửa tại chỗ và trả lại chính list đó. `ctx=None` -> chỉ chuẩn hóa `port_source`."""
    for s in series_list:
        if ctx is None:
            if s.port is not None and s.port_source == "unknown":
                s.port_source = "observed"
            continue
        infer_port(s, ctx)
        if s.path is not None and s.path_norm in ctx.probe_paths.get(
            (s.destination_namespace, s.destination_canonical_service), set()
        ):
            s.add_flag("probe_path")
        if (s.source_workload_namespace, s.source_service_account) in ctx.monitoring_identities:
            s.add_flag("verified_monitoring")
        if ctx.matches_serviceentry(s.destination_service_host):
            s.add_flag("serviceentry_host")
    return series_list
