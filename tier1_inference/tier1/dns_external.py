"""
DNS auto-allow (mục 7) và phân loại scope IN_CLUSTER/EXTERNAL/UNKNOWN (mục 8).

Cả hai đều là xử lý "rẽ nhánh trước khi vào scoring chính", không phải một phần của
công thức sigmoid — đúng tinh thần "xử lý riêng, không đưa vào scoring" của mục 7.
"""

from __future__ import annotations

from .aggregation import TrafficAggregate
from .config import ScoringConfig, DEFAULT_CONFIG
from .schema import IdentityKey, ServiceRef, Scope


def is_dns_edge(identity_key: IdentityKey, config: ScoringConfig = DEFAULT_CONFIG) -> bool:
    """mục 7: DNS là 'hidden dependency' kinh điển — Kube-dns/CoreDNS luôn auto-allow,
    không phụ thuộc kết luận Tier 1/Tier 2, dù port có tách được hay không."""
    return identity_key.port == config.dns_port or identity_key.protocol.upper() == "DNS"


def classify_scope(
    identity_key: IdentityKey,
    destination_service_ref: ServiceRef | None,
) -> Scope:
    """mục 8: scope in {IN_CLUSTER, EXTERNAL, UNKNOWN}.

    Heuristic (tài liệu không cho thuật toán chi tiết, đây là lựa chọn thiết kế của Tier 1,
    nhất quán với cách CONFIG_EVIDENCE_COLLECTOR.md mục 4 bước 6 tự phân loại EXTERNAL —
    "không khớp Service nội bộ, khớp dạng địa chỉ ngoài"):

      - Có destination_service_ref (đã đối chiếu ra đúng 1 K8s Service thật) -> IN_CLUSTER.
      - Không có service_ref NHƯNG destination_canonical_service có dạng FQDN
        (chứa dấu chấm, ví dụ "mysql.prod.example.com") -> EXTERNAL.
      - Còn lại (không service_ref, không giống FQDN) -> UNKNOWN, cần review thủ công
        thay vì đoán bừa.
    """
    if destination_service_ref is not None:
        return Scope.IN_CLUSTER
    if "." in identity_key.destination_canonical_service:
        return Scope.EXTERNAL
    return Scope.UNKNOWN
