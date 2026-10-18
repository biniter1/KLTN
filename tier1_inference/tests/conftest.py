"""Fixture & factory dùng chung cho toàn bộ test suite Tier 1."""

from __future__ import annotations

import pytest

from tier1.config import ScoringConfig
from tier1.schema import (
    ConfigEvidenceRecord,
    ConfigFinding,
    IdentityKey,
    Scope,
    ServiceRef,
    TrafficSeries,
)

NS = "default"

_UNSET = object()  # sentinel: phân biệt "không truyền tham số" với "truyền None tường minh"


@pytest.fixture(autouse=True)
def _no_real_image_reader(monkeypatch):
    """collect_config_evidence() mặc định đọc image thật (registry/docker, xem
    config_collector/image_config.py) -- tắt trong test để không gọi mạng. Test nào cần
    thì tự monkeypatch lại."""
    monkeypatch.setenv("CONFIG_COLLECTOR_IMAGE_READER", "none")


@pytest.fixture
def config() -> ScoringConfig:
    """Config mặc định, dùng chung cho hầu hết test — object mới mỗi test để tránh
    một test lỡ mutate config ảnh hưởng test khác."""
    return ScoringConfig()


def make_identity_key(
    dest: str = "productcatalogservice",
    source: str = "frontend",
    protocol: str = "GRPC",
    port: int | None = 3550,
    source_ns: str = NS,
    dest_ns: str = NS,
) -> IdentityKey:
    return IdentityKey(
        source_namespace=source_ns,
        source_canonical_service=source,
        source_service_account=f"{source}-sa",
        destination_namespace=dest_ns,
        destination_canonical_service=dest,
        protocol=protocol,
        port=port,
    )


def make_series(
    dest: str = "productcatalogservice",
    source: str = "frontend",
    protocol: str = "GRPC",
    port: int | None = 3550,
    path: str | None = "/Business/Call",
    count: int = 100,
    distinct_days: int = 5,
    last_seen_days_ago: float = 0.5,
    error_count: int = 0,
    source_workload_namespace: str = NS,
    source_ns: str = NS,
    dest_ns: str = NS,
    destination_service_ref=_UNSET,
    traffic_days=_UNSET,
    port_source: str | None = None,
) -> TrafficSeries:
    """destination_service_ref: bỏ qua tham số này -> tự tạo ServiceRef(dest, dest_ns) mặc định
    (tiện cho phần lớn test, giả định IN_CLUSTER). Truyền tường minh `None` -> giữ nguyên None
    (dùng khi test cố tình cần scope EXTERNAL/UNKNOWN, không có Service nội bộ nào khớp)."""
    if destination_service_ref is _UNSET:
        destination_service_ref = ServiceRef(dest, dest_ns)
    if traffic_days is _UNSET:
        # mặc định: `distinct_days` ngày liên tiếp kết thúc 08/10/2026
        traffic_days = [f"2026-10-{8 - i:02d}" for i in range(distinct_days)][::-1]
    if port_source is None:
        port_source = "observed" if port is not None else "unknown"
    return TrafficSeries(
        source_namespace=source_ns,
        source_canonical_service=source,
        source_service_account=f"{source}-sa",
        source_workload_namespace=source_workload_namespace,
        destination_namespace=dest_ns,
        destination_canonical_service=dest,
        destination_service_account=f"{dest}-sa",
        destination_service_ref=destination_service_ref,
        protocol=protocol,
        port=port,
        path=path,
        count=count,
        distinct_days=distinct_days if not traffic_days else len(traffic_days),
        last_seen_days_ago=last_seen_days_ago,
        error_count=error_count,
        traffic_days=list(traffic_days),
        port_source=port_source,
    )


def make_config_evidence(
    dest: str = "productcatalogservice",
    source: str = "frontend",
    protocol: str | None = "GRPC",
    port: int | None = 3550,
    finding: ConfigFinding = ConfigFinding.FOUND_EMBEDDED,
    scope: Scope = Scope.IN_CLUSTER,
    snapshot_id: str = "snap_test",
    source_ns: str = NS,
    dest_ns: str = NS,
) -> ConfigEvidenceRecord:
    return ConfigEvidenceRecord(
        source_namespace=source_ns,
        source_canonical_service=source,
        source_service_account=f"{source}-sa",
        destination_namespace=dest_ns,
        destination_canonical_service=dest,
        destination_service_account=f"{dest}-sa",
        protocol=protocol,
        port=port,
        port_unknown=(port is None),
        config_finding=finding,
        evidence_source="env",
        scope=scope,
        snapshot_id=snapshot_id,
        collected_at="2026-09-19T00:00:00Z",
    )
