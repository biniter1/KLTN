"""
Gộp TrafficSeries (đã classify) lên cấp edge (1 identity_key) — chuẩn bị input cho scoring.py.

Không có trong tài liệu dưới một cái tên riêng, nhưng là bước bắt buộc để đi từ
"nhiều series Prometheus" (TRAFFIC_COLLECTION_PIPELINE.md) sang "1 dòng evidence
mỗi edge" (TIER1_DESIGN.md mục 10) — xem chú thích trong schema.py::TrafficSeries.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .schema import IdentityKey, ServiceRef, TrafficClass, TrafficSeries


@dataclass
class TrafficAggregate:
    """1 dòng / 1 identity_key, tổng hợp từ toàn bộ series thuộc edge đó."""

    identity_key: IdentityKey
    destination_service_ref: ServiceRef | None

    count: int = 0
    error_count: int = 0
    distinct_days: int = 0
    last_seen_days_ago: float = float("inf")
    distinct_protocols: set[str] = field(default_factory=set)
    workload_revision: str | None = None

    class_counts: dict[TrafficClass, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def error_ratio(self) -> float:
        return (self.error_count / self.count) if self.count else 0.0

    def class_ratio(self, cls: TrafficClass) -> float:
        return (self.class_counts.get(cls, 0) / self.count) if self.count else 0.0

    def dominant_traffic_class(self) -> TrafficClass:
        """traffic_class ở mức edge (mục 10) = class chiếm tỷ trọng traffic cao nhất.
        Hòa nhau -> ưu tiên BUSINESS nếu có mặt, tránh việc noise nhỏ (1-2 request healthcheck
        lẫn vào) làm mất nhãn BUSINESS một cách bất công."""
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
            )
            result[key] = agg

        agg.count += s.count
        agg.error_count += s.error_count
        agg.distinct_days = max(agg.distinct_days, s.distinct_days)
        agg.last_seen_days_ago = min(agg.last_seen_days_ago, s.last_seen_days_ago)
        agg.distinct_protocols.add(s.protocol.upper())
        agg.class_counts[s.traffic_class] += s.count
        # revision mới nhất ghi đè — temporal drift (TRAFFIC_COLLECTION_PIPELINE.md mục 4)
        if s.workload_revision:
            agg.workload_revision = s.workload_revision

    return result
