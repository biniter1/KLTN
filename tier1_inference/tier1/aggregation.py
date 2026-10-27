"""
Gộp TrafficSeries (đã phân loại) lên cấp CẠNH (1 identity_key) — TIER1_DESIGN.md mục 2b.

Khóa cạnh = identity_key 7 phần (nguồn, SA nguồn, đích, protocol, port) — KHÔNG có path:
NetworkPolicy/AuthorizationPolicy cấp quyền theo cặp workload + port, không theo path.
Cơ chế: dict[IdentityKey -> TrafficAggregate], duyệt mỗi series 1 lần.

Cách gộp từng trường (chốt 15/10/2026):
  count, error_count       cộng
  traffic_days             HỢP các ngày (bản cũ lấy max số ngày -> cạnh nhiều path thưa
                           bị tính thiếu ngày, vd /export ngày 02 + /sync ngày 05 = 2 ngày)
  distinct_days            len(traffic_days); series từ file JSON cũ không có traffic_days
                           -> quay về max (cờ `legacy_days`)
  last_seen_days_ago       min
  class_counts[class]      += series.count (trọng số theo lưu lượng)
  flags                    hợp
  workload_revision        revision mới nhất ghi đè
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .schema import IdentityKey, ServiceRef, TrafficClass, TrafficSeries

NOISE_CLASSES = frozenset({TrafficClass.HEALTHCHECK, TrafficClass.MONITORING, TrafficClass.CONTROL_PLANE})


@dataclass
class TrafficAggregate:
    """1 dòng / 1 identity_key, tổng hợp từ toàn bộ series thuộc cạnh đó."""

    identity_key: IdentityKey
    destination_service_ref: ServiceRef | None

    count: int = 0
    error_count: int = 0
    traffic_days: set[str] = field(default_factory=set)
    legacy_max_days: int = 0  # chỉ dùng khi có series thiếu traffic_days (file JSON cũ)
    last_seen_days_ago: float = float("inf")
    distinct_protocols: set[str] = field(default_factory=set)  # giữ cho Tier 2 (traffic window)
    workload_revision: str | None = None
    port_source: str = "unknown"
    flags: set[str] = field(default_factory=set)
    destination_service_host: str | None = None

    class_counts: dict[TrafficClass, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def distinct_days(self) -> int:
        return max(len(self.traffic_days), self.legacy_max_days)

    @property
    def error_ratio(self) -> float:
        return (self.error_count / self.count) if self.count else 0.0

    def class_ratio(self, cls: TrafficClass) -> float:
        return (self.class_counts.get(cls, 0) / self.count) if self.count else 0.0

    @property
    def is_noise_only(self) -> bool:
        """Cạnh chỉ toàn nhiễu: có traffic nhưng KHÔNG có request BUSINESS/UNKNOWN nào
        (mục 5, nhánh nhiễu — chốt 15/10)."""
        if not self.count:
            return False
        return all(cls in NOISE_CLASSES for cls, n in self.class_counts.items() if n > 0)

    def dominant_traffic_class(self) -> TrafficClass:
        """traffic_class ở mức cạnh (mục 10) = class chiếm tỷ trọng traffic cao nhất.
        BUSINESS ≥ class đứng nhì -> BUSINESS, tránh vài request healthcheck làm mất nhãn
        BUSINESS một cách bất công."""
        if not self.class_counts:
            return TrafficClass.UNKNOWN
        if self.class_counts.get(TrafficClass.BUSINESS, 0) > 0:
            top_non_business = max(
                (c for c in self.class_counts if c != TrafficClass.BUSINESS),
                key=lambda c: self.class_counts[c],
                default=None,
            )
            if top_non_business is None or self.class_counts[TrafficClass.BUSINESS] >= self.class_counts[top_non_business]:
                return TrafficClass.BUSINESS
        return max(self.class_counts, key=lambda c: self.class_counts[c])


_PORT_SOURCE_RANK = {"observed": 0, "observed_log": 1, "inferred_single_port": 2, "inferred_protocol_match": 3, "config": 4, "unknown": 9}


def aggregate_series(series_list: list[TrafficSeries]) -> dict[IdentityKey, TrafficAggregate]:
    """Yêu cầu series_list đã được classification.classify_all() gắn traffic_class trước đó."""

    result: dict[IdentityKey, TrafficAggregate] = {}

    for s in series_list:
        if s.traffic_class is None:
            raise ValueError(
                f"TrafficSeries chưa được classify (traffic_class=None): {s!r}. "
                "Gọi classification.classify_all() trước aggregate_series()."
            )
        key = s.identity_key()
        agg = result.get(key)
        if agg is None:
            agg = TrafficAggregate(
                identity_key=key,
                destination_service_ref=s.destination_service_ref,
                workload_revision=s.workload_revision,
                port_source=s.port_source,
                destination_service_host=s.destination_service_host,
            )
            result[key] = agg

        agg.count += s.count
        agg.error_count += s.error_count
        if s.traffic_days:
            agg.traffic_days.update(s.traffic_days)
        else:
            agg.legacy_max_days = max(agg.legacy_max_days, s.distinct_days)
            if s.distinct_days:
                agg.flags.add("legacy_days")
        agg.last_seen_days_ago = min(agg.last_seen_days_ago, s.last_seen_days_ago)
        agg.distinct_protocols.add(s.protocol.upper())
        agg.class_counts[s.traffic_class] += s.count
        agg.flags.update(s.flags)
        if _PORT_SOURCE_RANK.get(s.port_source, 9) < _PORT_SOURCE_RANK.get(agg.port_source, 9):
            agg.port_source = s.port_source
        if agg.destination_service_ref is None and s.destination_service_ref is not None:
            agg.destination_service_ref = s.destination_service_ref
        if agg.destination_service_host is None:
            agg.destination_service_host = s.destination_service_host
        # revision mới nhất ghi đè — temporal drift (TRAFFIC_COLLECTION_PIPELINE.md mục 4)
        if s.workload_revision:
            agg.workload_revision = s.workload_revision

    return result
