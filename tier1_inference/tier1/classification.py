"""
Phân loại traffic_class cho từng TrafficSeries — TIER1_DESIGN.md mục 2.2 (chốt 15/10/2026).

Chạy Ở CẤP SERIES (mỗi series là 1 API: nguồn, đích, protocol, port, path), TRƯỚC khi
gộp lên cấp cạnh (aggregation.py) — chỉ ở đây còn path để phân biệt healthcheck với
nghiệp vụ. Không xóa series nào — chỉ gắn nhãn để scoring.py biết loại khỏi phần đóng
góp tích cực vào điểm số.

Thứ tự luật (luật đầu tiên khớp thì dừng) và lý do:

    1. DNS            đích là Service DNS của cluster (kube-system/kube-dns) hoặc port 53
                      — đặc trưng chắc nhất; cạnh DNS do Planner tự mở (mục 7).
    2. MONITORING     CHỈ khi danh tính ĐÃ XÁC MINH: (namespace, service account) lấy từ
                      SPIFFE principal (mTLS) — collector gắn cờ `verified_monitoring`
                      (tự phát hiện Prometheus/vmagent) hoặc khớp
                      `verified_monitoring_identities`. Đứng TRƯỚC CONTROL_PLANE
                      (Prometheus thật ở istio-system) và TRƯỚC HEALTHCHECK (Prometheus
                      gọi /metrics) — bản cũ đặt sau nên không bao giờ gắn được.
    3. CONTROL_PLANE  namespace NGUỒN hoặc ĐÍCH ∈ control_plane_namespaces.
       (gắn cờ)       tên nguồn trùng `monitoring_names` mà danh tính không xác minh →
                      cờ `claimed_monitoring`, KHÔNG trừ điểm (chống giả tên).
    4. HEALTHCHECK    3 tầng: path probe của đúng service đích (cờ `probe_path`) ·
                      segment cuối ∈ danh sách mặc định ngắn · gRPC health/reflection ·
                      path đội vận hành tự thêm. So theo segment, KHÔNG theo tiền tố.
    5. BUSINESS       protocol HTTP/GRPC/TCP còn lại — nghiêng về giữ cạnh (ưu tiên recall).
    6. UNKNOWN        còn lại.
"""

from __future__ import annotations

import fnmatch

from .config import DEFAULT_CONFIG, ScoringConfig
from .schema import TrafficClass, TrafficSeries, normalize_path


def is_dns(series: TrafficSeries, config: ScoringConfig = DEFAULT_CONFIG) -> bool:
    if series.protocol.upper() == "DNS" or series.port == config.dns_port:
        return True
    ref = series.destination_service_ref
    return ref is not None and f"{ref.service_namespace}/{ref.service_name}" in config.dns_services


def is_verified_monitoring(series: TrafficSeries, config: ScoringConfig = DEFAULT_CONFIG) -> bool:
    if "verified_monitoring" in series.flags:
        return True
    sa = series.source_service_account
    if not sa or sa == "unknown":
        return False  # nguồn ngoài mesh -> không có SPIFFE principal -> không xác minh được
    identity = f"{series.source_workload_namespace}/{sa}"
    return any(fnmatch.fnmatchcase(identity, pat) for pat in config.verified_monitoring_identities)


def is_healthcheck(series: TrafficSeries, config: ScoringConfig = DEFAULT_CONFIG) -> bool:
    if series.path is None:
        return False
    if "probe_path" in series.flags:
        return True
    path = series.path_norm
    segments = series.path_segments
    if not segments:
        return False
    # gRPC: path dạng /<package.Service>/<Method> -> so tên service (segment đầu)
    if segments[0] in config.healthcheck_grpc_services:
        return True
    if segments[-1] in config.healthcheck_last_segments:
        return True
    extra = {normalize_path(p) for p in config.healthcheck_extra_paths}
    return path in extra


def classify_series(series: TrafficSeries, config: ScoringConfig = DEFAULT_CONFIG) -> TrafficClass:
    """Trả về traffic_class cho một series. Chỉ có 1 tác dụng phụ: gắn cờ
    `claimed_monitoring` (luật 3) — pipeline.py gán traffic_class."""
    if is_dns(series, config):
        return TrafficClass.DNS

    if is_verified_monitoring(series, config):
        return TrafficClass.MONITORING

    if (series.source_workload_namespace in config.control_plane_namespaces
            or series.destination_namespace in config.control_plane_namespaces):
        return TrafficClass.CONTROL_PLANE

    if series.source_canonical_service.lower() in config.monitoring_names:
        series.add_flag("claimed_monitoring")

    if is_healthcheck(series, config):
        return TrafficClass.HEALTHCHECK

    if series.protocol.upper() in ("HTTP", "GRPC", "TCP"):
        return TrafficClass.BUSINESS

    return TrafficClass.UNKNOWN


def classify_all(series_list: list[TrafficSeries], config: ScoringConfig = DEFAULT_CONFIG) -> list[TrafficSeries]:
    """Gắn traffic_class cho toàn bộ danh sách series (in-place) và trả lại chính list đó."""
    for s in series_list:
        s.traffic_class = classify_series(s, config)
    return series_list
