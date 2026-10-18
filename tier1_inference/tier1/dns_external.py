"""
Phân loại scope IN_CLUSTER / EXTERNAL / UNKNOWN — TIER1_DESIGN.md mục 8 (chốt 15/10/2026).

DNS (mục 7) KHÔNG còn quyết ở đây: chỉ 1 chỗ quyết DNS là bước phân loại
(`classification.is_dns`, gán `traffic_class == DNS`); pipeline tách DNS theo traffic_class.
Bản cũ có thêm `is_dns_edge` (port 53) chạy song song — 2 luật DNS ở 2 nơi.

Thứ tự xác định scope cho cạnh traffic:
  1. Istio ghi `destination_service = PassthroughCluster` (ra ngoài, chưa khai ServiceEntry)
     hoặc `BlackHoleCluster` (bị chặn) -> EXTERNAL, cờ `external_unregistered`
  2. Host đích khớp 1 ServiceEntry (index cluster, có wildcard) -> EXTERNAL
  3. Có destination_service_ref (đúng 1 K8s Service thật) -> IN_CLUSTER
  4. Tên đích có dấu chấm (dạng FQDN) -> EXTERNAL (phương án cuối)
  5. Còn lại -> UNKNOWN, cần người xem thay vì đoán
Cờ (1), (2) do collector gắn lúc thu thập (traffic_collector.py / cluster_context.py).
"""

from __future__ import annotations

from typing import Iterable

from .schema import IdentityKey, Scope, ServiceRef

EXTERNAL_FLAGS = frozenset({"external_unregistered", "serviceentry_host"})


def classify_scope(
    identity_key: IdentityKey,
    destination_service_ref: ServiceRef | None,
    flags: Iterable[str] = (),
) -> Scope:
    if EXTERNAL_FLAGS.intersection(flags):
        return Scope.EXTERNAL
    if destination_service_ref is not None:
        return Scope.IN_CLUSTER
    if "." in (identity_key.destination_canonical_service or ""):
        return Scope.EXTERNAL
    return Scope.UNKNOWN
