"""
Phân loại traffic_class cho từng TrafficSeries — TIER1_DESIGN.md mục 2.

Chạy Ở CẤP SERIES (mỗi series là 1 path/protocol riêng biệt), TRƯỚC khi gộp lên cấp
edge (aggregation.py). Không xóa record nào — chỉ gắn nhãn để scoring.py biết loại
khỏi phần đóng góp tích cực vào điểm số (mục 2: "vẫn phục vụ được việc gỡ lỗi khi cần").

Thứ tự rule CÓ Ý NGHĨA — áp dụng đúng thứ tự ưu tiên mô tả trong tài liệu:
DNS > CONTROL_PLANE > HEALTHCHECK > MONITORING > BUSINESS > UNKNOWN (fallback).
"""

from __future__ import annotations

from .config import ScoringConfig, DEFAULT_CONFIG
from .schema import TrafficClass, TrafficSeries


def classify_series(series: TrafficSeries, config: ScoringConfig = DEFAULT_CONFIG) -> TrafficClass:
    """Trả về traffic_class cho một series, KHÔNG mutate input (pipeline.py gán lại)."""

    # DNS: destination port = 53 (mục 2)
    if series.port == config.dns_port or series.protocol.upper() == "DNS":
        return TrafficClass.DNS

    # CONTROL_PLANE: namespace nguồn thuộc istio-system/kube-system (mục 2)
    if series.source_workload_namespace in config.control_plane_namespaces:
        return TrafficClass.CONTROL_PLANE

    # HEALTHCHECK: path khớp /health*, /ready*, /live*, /metrics (mục 2), CỘNG THÊM
    # convention gRPC health-check chuẩn (grpc.health.v1.Health/Check) — mở rộng dựa
    # trên dữ liệu thật của app nền, đúng tinh thần "heuristic ban đầu, tinh chỉnh khi
    # có dữ liệu thật" của mục 2.
    if series.path is not None:
        path_lower = series.path.lower()
        if any(path_lower.startswith(p) for p in config.healthcheck_path_prefixes):
            return TrafficClass.HEALTHCHECK
        if any(sub in path_lower for sub in config.healthcheck_path_contains):
            return TrafficClass.HEALTHCHECK

    # MONITORING: source workload nằm trong danh sách đã biết (mục 2)
    if series.source_canonical_service.lower() in config.monitoring_source_services:
        return TrafficClass.MONITORING

    # BUSINESS: mặc định cho traffic có path/protocol hợp lệ không khớp rule nào ở trên
    if series.protocol.upper() in ("HTTP", "GRPC", "TCP"):
        return TrafficClass.BUSINESS

    # UNKNOWN: không khớp rule nào rõ ràng (mục 2) — vẫn tính vào evidence nhưng hạ độ tin
    return TrafficClass.UNKNOWN


def classify_all(series_list: list[TrafficSeries], config: ScoringConfig = DEFAULT_CONFIG) -> list[TrafficSeries]:
    """Gắn traffic_class cho toàn bộ danh sách series (in-place) và trả lại chính list đó."""
    for s in series_list:
        s.traffic_class = classify_series(s, config)
    return series_list
