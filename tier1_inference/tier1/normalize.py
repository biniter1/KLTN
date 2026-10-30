"""
Chuẩn hóa dữ liệu ĐẦU VÀO trước khi phân loại — TIER1_CHANGE_PLAN.md mục B3.

Hai việc tách riêng khỏi `classification.py`/`traffic_collector.py` vì đều là phép
biến đổi THUẦN DỮ LIỆU, không phụ thuộc ngưỡng/quy tắc nghiệp vụ nào (đúng nguyên tắc
"tách config khỏi logic" ở README.md — ở đây là "tách chuẩn hóa khỏi logic"):

1. `normalize_path()` / `path_segments()` — chuẩn hóa `TrafficSeries.path` (viết
   thường, cắt query string, bỏ `/` cuối) + tách segment, để `classification.py` (C4)
   và `candidates.py`/healthcheck-matching so sánh path một cách NHẤT QUÁN (tránh
   "/Health" và "/health?x=1" bị coi là khác nhau trong khi cùng 1 endpoint).

2. `infer_port()` — suy port khi traffic KHÔNG mang port rõ ràng (TCP Envoy đôi khi
   chỉ phơi port đích thật; nhưng khi phải suy từ `destination_service_ref`, dùng
   đúng logic mục B3: 1 port duy nhất trên Service -> `inferred_single_port`; nhiều
   port -> khớp theo `appProtocol`/tên cổng quy ước (`grpc-*`, `http-*`, `tcp-*`) với
   protocol đã biết của traffic -> `inferred_protocol_match`; không khớp được gì ->
   `None` + `"unknown"` (KHÔNG đoán bừa — bịa 1 port sai sẽ làm sai cả config-matching
   ở giai đoạn D, tệ hơn là để trống)).

   Dùng cùng `ClusterSnapshot`/`ServiceInfo` của Collector (`config_collector.validate`,
   `config_collector.k8s_types`) để không phải tự viết lại 1 bộ đọc Service riêng ở
   Tier 1 — đúng nguyên tắc "1 nguồn sự thật cho mỗi loại dữ liệu cụm".
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from .config_collector.k8s_types import ServiceInfo
from .schema import ServiceRef


# ---------------------------------------------------------------------------
# 1. Path
# ---------------------------------------------------------------------------

def normalize_path(path: Optional[str]) -> Optional[str]:
    """Viết thường, cắt query string (`?...`)/fragment (`#...`), bỏ `/` ở cuối (trừ
    khi path chỉ là `"/"`). `None`/rỗng -> `None` (TCP/DNS thuần không có path)."""
    if not path:
        return None
    stripped = urlsplit(path).path or path.split("?")[0].split("#")[0]
    normalized = stripped.strip().lower()
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized or None


def path_segments(path_norm: Optional[str]) -> list[str]:
    """Tách `path_norm` (đã qua `normalize_path`) thành các segment không rỗng.
    Dùng khi so khớp "segment cuối" (C4: healthcheck) hay "segment đầu, tên service
    gRPC" (ví dụ `/grpc.health.v1.Health/Check` -> `["grpc.health.v1.health", "check"]`)."""
    if not path_norm:
        return []
    return [seg for seg in path_norm.split("/") if seg]


# ---------------------------------------------------------------------------
# 2. Port
# ---------------------------------------------------------------------------

# Tiền tố tên cổng quy ước Kubernetes/Istio (Istio tự suy protocol từ tiền tố này khi
# không có appProtocol khai báo — mục B3 "khớp ... tên grpc-*/http-*").
_PORT_NAME_PROTOCOL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("grpc-", "GRPC"),
    ("grpc", "GRPC"),
    ("http2-", "GRPC"),  # Istio: http2 không TLS thường dùng cho gRPC thuần
    ("http-", "HTTP"),
    ("https-", "HTTP"),
    ("tcp-", "TCP"),
    ("udp-", "UDP"),
)


def _port_protocol_hint(port) -> Optional[str]:
    """Suy "protocol" 1 ServicePortInfo đại diện, từ appProtocol trước, tên cổng sau."""
    if port.app_protocol:
        return port.app_protocol.upper()
    name = (port.name or "").lower()
    for prefix, proto in _PORT_NAME_PROTOCOL_PREFIXES:
        if name.startswith(prefix):
            return proto
    return None


def infer_port(
    service: Optional[ServiceInfo],
    observed_port: Optional[int],
    protocol: Optional[str] = None,
) -> tuple[Optional[int], str]:
    """Mục B3. Trả `(port, port_source)`.

    - Đã quan sát được port thật (`observed_port` không None) -> trả nguyên, nguồn
      "observed" (không cần suy gì — đây là trường hợp đa số, TCP/HTTP/GRPC qua Envoy
      thường đã có port đích trong nhãn).
    - Không có Service (chưa phân giải được `destination_service_ref`, hoặc Service
      không có port nào khai báo) -> `(None, "unknown")`.
    - Service có ĐÚNG 1 port -> suy thẳng port đó, nguồn "inferred_single_port" (không
      cần biết protocol traffic là gì — chỉ có 1 lựa chọn).
    - Service có NHIỀU port -> khớp theo `protocol` traffic đã biết (so với appProtocol/
      tên cổng quy ước của từng port, mục `_port_protocol_hint`). Khớp ĐÚNG 1 port ->
      "inferred_protocol_match". Khớp 0 hoặc >1 port (mơ hồ) -> `(None, "unknown")`
      (không đoán bừa khi không chắc — xem docstring đầu file)."""
    if observed_port is not None:
        return observed_port, "observed"

    if service is None or not service.ports:
        return None, "unknown"

    if len(service.ports) == 1:
        return service.ports[0].target_port, "inferred_single_port"

    if protocol:
        proto_upper = protocol.upper()
        matches = [p for p in service.ports if _port_protocol_hint(p) == proto_upper]
        if len(matches) == 1:
            return matches[0].target_port, "inferred_protocol_match"

    return None, "unknown"


def find_service(
    services: "tuple[ServiceInfo, ...] | list[ServiceInfo]", service_ref: Optional[ServiceRef],
) -> Optional[ServiceInfo]:
    """Tiện ích tra `ServiceInfo` từ `destination_service_ref` (B2) trong danh sách
    Service của 1 `ClusterSnapshot` (Collector `config_collector.validate`). Tách
    riêng để `traffic_collector.py` không phải tự lặp vòng for mỗi lần gọi `infer_port`."""
    if service_ref is None:
        return None
    return next(
        (s for s in services if s.namespace == service_ref.service_namespace and s.name == service_ref.service_name),
        None,
    )
