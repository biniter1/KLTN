"""Nhận input escalate từ Tier 1 (TIER1_DESIGN.md mục 10, 11) — không sửa,
không thêm/bớt field, đúng cam kết mục 0 của TIER2_DESIGN.md.

`EscalationRecord.raw` giữ nguyên dict gốc Tier 1 gửi sang để build `goal`
(mục 8.1: "goal đưa vào Model gồm: schema escalate gốc của Tier 1 +
escalation_reason") — không có bước nào ở Tier 2 được phép sửa nó.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .exceptions import InvalidEscalationError
from .models import DestinationServiceRef

# escalation_reason — bảng tra cứu thuần code, không LLM (mục 8.1)
ESCALATION_REASON_LOOKUP = {
    "NOT_OBSERVED": (
        "Nghi CronJob chu kỳ dài / evidence đã lỗi thời / dependency tạo động "
        "lúc runtime"
    ),
    "OBSERVED_WEAK": "Nghi chu kỳ ngắn hơn cửa sổ K ngày, hoặc nhánh lỗi hiếm",
    "UNKNOWN": "Không phải vấn đề tần suất, mà là vấn đề PHÂN LOẠI/Ý NGHĨA",
}

# Trạng thái config_finding coi là "mù" — cùng NOT_OBSERVED thì mới escalate (mục 0)
_BLIND_CONFIG_FINDINGS = frozenset({"FOUND_UNRESOLVED", "NOT_FOUND"})

_REQUIRED_FIELDS = (
    "source_namespace",
    "source_canonical_service",
    "source_service_account",
    "destination_namespace",
    "destination_canonical_service",
    "protocol",
    "port",
    "dependency_status",
)


@dataclass(frozen=True)
class EscalationRecord:
    source_namespace: str
    source_canonical_service: str
    source_service_account: str
    destination_namespace: str
    destination_canonical_service: str
    destination_service_ref: Optional[DestinationServiceRef]
    protocol: str
    port: int
    dependency_status: str
    config_finding: Optional[str]
    raw: dict = field(repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "EscalationRecord":
        missing = [f for f in _REQUIRED_FIELDS if f not in data]
        if missing:
            raise InvalidEscalationError(
                f"Thiếu field bắt buộc trong escalation record: {missing}"
            )
        ref = data.get("destination_service_ref")
        dest_ref = (
            DestinationServiceRef(
                service_name=ref["service_name"],
                service_namespace=ref["service_namespace"],
            )
            if ref
            else None
        )
        return cls(
            source_namespace=data["source_namespace"],
            source_canonical_service=data["source_canonical_service"],
            source_service_account=data["source_service_account"],
            destination_namespace=data["destination_namespace"],
            destination_canonical_service=data["destination_canonical_service"],
            destination_service_ref=dest_ref,
            protocol=data["protocol"],
            port=data["port"],
            dependency_status=data["dependency_status"],
            config_finding=data.get("config_finding"),
            raw=dict(data),
        )

    @property
    def identity_key(self) -> str:
        """identity_key ổn định (TIER1_DESIGN.md mục 3) — dùng làm
        `edge_identity_key` của State (TIER2_DESIGN.md mục 5)."""

        parts = (
            self.source_namespace,
            self.source_canonical_service,
            self.source_service_account,
            self.destination_namespace,
            self.destination_canonical_service,
            self.protocol,
            str(self.port),
        )
        return "|".join(parts)

    @property
    def canonical_services(self) -> frozenset:
        """2 canonical_service hợp lệ của đúng cạnh đang xác minh.

        NOTE (xem README "Điểm mở/diễn giải"): TIER2_DESIGN.md mục 13 để ngỏ
        câu hỏi whitelist `source_service`/`service` nên giới hạn 2 service
        của cạnh, hay toàn bộ service Tier 1 cung cấp. Cài đặt này chọn
        phương án CHẶT (chỉ 2 service của cạnh) vì đó là phương án an toàn
        hơn và không cần thông tin gì thêm ngoài escalation record.
        """

        return frozenset({self.source_canonical_service, self.destination_canonical_service})

    def should_escalate(self) -> bool:
        """Điều kiện kích hoạt Tier 2 (mục 0)."""

        if self.dependency_status in ("OBSERVED_WEAK", "UNKNOWN"):
            return True
        if self.dependency_status == "NOT_OBSERVED":
            return self.config_finding in _BLIND_CONFIG_FINDINGS
        return False

    @property
    def escalation_reason(self) -> str:
        return ESCALATION_REASON_LOOKUP.get(self.dependency_status, "Không xác định")
