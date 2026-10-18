"""
Kiểu dữ liệu dùng chung cho toàn bộ Tier 1.

Đối chiếu tài liệu thiết kế:
- TrafficClass, Scope, ConfigFinding, DependencyStatus   -> TIER1_DESIGN.md mục 2, 5, 8
- IdentityKey                                            -> TIER1_DESIGN.md mục 3
- TrafficSeries                                          -> đầu vào từ TRAFFIC_COLLECTION_PIPELINE.md
                                                             (một "series" = một tổ hợp nhãn duy nhất
                                                             mà Envoy/Prometheus phơi ra, đã qua increase())
- ConfigEvidenceRecord                                   -> đầu ra của CONFIG_EVIDENCE_COLLECTOR.md mục 6
- DependencyEdgeResult                                   -> TIER1_DESIGN.md mục 10 (schema evidence đầy đủ)
                                                             + vài cột nghiệp vụ nội bộ (previous_status,
                                                             escalate_to_tier2, needs_human_review...)

Toàn bộ dataclass ở đây CHỈ là hình dạng dữ liệu (domain-neutral: không có field nào
gắn cứng với "NetworkPolicy"). Việc sinh NetworkPolicy YAML là việc của một module tiêu thụ
DependencyEdgeResult ở tầng sau (Migration Planner, ngoài phạm vi Tier 1) — xem README.md
phần "Điểm flex" để biết vì sao tách như vậy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Enums — mục 2, 5, 8 TIER1_DESIGN.md ; mục 6 CONFIG_EVIDENCE_COLLECTOR.md
# ---------------------------------------------------------------------------

class TrafficClass(str, Enum):
    BUSINESS = "BUSINESS"
    HEALTHCHECK = "HEALTHCHECK"
    MONITORING = "MONITORING"
    DNS = "DNS"
    CONTROL_PLANE = "CONTROL_PLANE"
    UNKNOWN = "UNKNOWN"


class Scope(str, Enum):
    IN_CLUSTER = "IN_CLUSTER"
    EXTERNAL = "EXTERNAL"
    UNKNOWN = "UNKNOWN"


class ConfigFinding(str, Enum):
    FOUND_STRUCTURAL = "FOUND_STRUCTURAL"
    FOUND_EMBEDDED = "FOUND_EMBEDDED"
    FOUND_UNRESOLVED = "FOUND_UNRESOLVED"
    NOT_FOUND = "NOT_FOUND"

    @property
    def is_clean_match(self) -> bool:
        """FOUND_STRUCTURAL/FOUND_EMBEDDED = đã đối chiếu ngược K8s thành công (mục 6 collector)."""
        return self in (ConfigFinding.FOUND_STRUCTURAL, ConfigFinding.FOUND_EMBEDDED)


class DependencyStatus(str, Enum):
    OBSERVED_STRONG = "OBSERVED_STRONG"
    OBSERVED_WEAK = "OBSERVED_WEAK"
    NOT_OBSERVED = "NOT_OBSERVED"
    UNKNOWN = "UNKNOWN"
    NEEDS_MANUAL_SELECTOR = "NEEDS_MANUAL_SELECTOR"


# ---------------------------------------------------------------------------
# Identity — TIER1_DESIGN.md mục 3
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdentityKey:
    """
    Key chuẩn hóa cho một edge dependency. Bất biến (frozen) vì được dùng làm dict key
    xuyên suốt pipeline (candidate set, previous_status map...).

    Lưu ý mục 3: canonical_service và service_account KHÔNG tự suy ra ở Tier 1 — chúng
    được collector (traffic pipeline / config collector) resolve sẵn từ nhãn Istio /
    K8s API trước khi đưa vào đây. Tier 1 chỉ tiêu thụ, không tự resolve.
    """

    source_namespace: str
    source_canonical_service: str
    source_service_account: str
    destination_namespace: str
    destination_canonical_service: str
    protocol: str
    port: Optional[int]  # None nếu chưa tách được port (evidence cấp 3, xem collector mục 4)

    def pair_key(self) -> Tuple[str, str, str, str]:
        """(source_ns, source_svc, dest_ns, dest_svc) — dùng để khớp C_observed với C_potential,
        vì C_potential (từ config evidence) không nhất thiết có protocol/port (mục 1)."""
        return (
            self.source_namespace,
            self.source_canonical_service,
            self.destination_namespace,
            self.destination_canonical_service,
        )

    def as_dict(self) -> dict:
        return {
            "source_namespace": self.source_namespace,
            "source_canonical_service": self.source_canonical_service,
            "source_service_account": self.source_service_account,
            "destination_namespace": self.destination_namespace,
            "destination_canonical_service": self.destination_canonical_service,
            "protocol": self.protocol,
            "port": self.port,
        }


@dataclass(frozen=True)
class ServiceRef:
    """destination_service_ref — TIER1_DESIGN.md mục 3b: trỏ thẳng K8s Service thật,
    tách biệt khỏi canonical_service (đã bị Istio "nén" nhiều workload lại)."""

    service_name: str
    service_namespace: str


# ---------------------------------------------------------------------------
# Traffic evidence — TRAFFIC_COLLECTION_PIPELINE.md
# ---------------------------------------------------------------------------

@dataclass
class TrafficSeries:
    """
    Một "series" = một tổ hợp nhãn Prometheus duy nhất sau increase() trong cửa sổ K ngày
    (một cặp source/dest có thể có NHIỀU series khác nhau, ví dụ 1 series /health và
    N series các path nghiệp vụ khác nhau — đó là lý do phân loại traffic_class chạy
    Ở CẤP SERIES rồi mới gộp lên cấp edge, đúng mục 2: "không xóa, chỉ loại khỏi phần
    đóng góp tích cực vào điểm số").

    is_injected/injection_id/workload_revision (mục 3, 4 TRAFFIC_COLLECTION_PIPELINE.md)
    được giữ nhưng không bắt buộc cho pipeline suy luận — chỉ cần cho phần đánh giá
    thí nghiệm (R=15 runs), nên để optional.
    """

    source_namespace: str
    source_canonical_service: str
    source_service_account: str
    source_workload_namespace: str  # namespace vật lý của pod nguồn -> dùng nhận CONTROL_PLANE
    destination_namespace: str
    destination_canonical_service: str
    destination_service_account: Optional[str]
    destination_service_ref: Optional[ServiceRef]
    protocol: str  # "HTTP" | "GRPC" | "TCP" | "DNS"
    port: int
    path: Optional[str]  # None cho TCP/DNS thuần

    count: int
    distinct_days: int
    last_seen_days_ago: float
    error_count: int = 0

    # provenance thí nghiệm (mục 3 TRAFFIC_COLLECTION_PIPELINE.md) — optional cho pipeline suy luận
    is_injected: bool = False
    injection_id: Optional[str] = None
    workload_revision: Optional[str] = None

    # gán bởi classification.py, để trống lúc khởi tạo
    traffic_class: Optional[TrafficClass] = field(default=None)

    def identity_key(self) -> IdentityKey:
        return IdentityKey(
            source_namespace=self.source_namespace,
            source_canonical_service=self.source_canonical_service,
            source_service_account=self.source_service_account,
            destination_namespace=self.destination_namespace,
            destination_canonical_service=self.destination_canonical_service,
            protocol=self.protocol,
            port=self.port,
        )

    @property
    def error_ratio(self) -> float:
        return (self.error_count / self.count) if self.count else 0.0


# ---------------------------------------------------------------------------
# Config evidence — CONFIG_EVIDENCE_COLLECTOR.md mục 6 (v2, LLM-assisted, 29/09/2026)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceRef:
    """1 nơi trong config trỏ tới cặp (source, dest) này — mục 6 `evidence_refs`.
    Một cặp có thể có NHIỀU evidence_refs (nhiều biến/dòng config cùng trỏ 1 đích)."""

    item_id: str
    source_kind: str  # "env" | "envFrom" | "command" | "args" | "init_env" | "configmap_file" | "image_env" | ...
    container: str
    key: str
    quote: str  # trích nguyên văn (mục 4.4a grounding) — rỗng cho baseline regex (không có bước grounding riêng)


@dataclass
class ConfigEvidenceRecord:
    source_namespace: str
    source_canonical_service: str
    source_service_account: str
    destination_namespace: Optional[str]  # None CHỈ khi FOUND_UNRESOLVED không phân giải
                                          # được identity đích thật (mục 6 v2 — JSON mẫu
                                          # "null nếu FOUND_UNRESOLVED không phân giải
                                          # được"). v1 (regex_baseline.py) KHÔNG BAO GIỜ
                                          # trả None ở đây (luôn có fallback dest_namespace
                                          # = namespace nguồn) nên không có record v1 nào
                                          # bị ảnh hưởng — đây là mở rộng kiểu THÊM khả
                                          # năng (str vẫn hợp lệ), không phải thu hẹp.
    destination_canonical_service: Optional[str]  # cùng lý do -- xem comment destination_namespace
    destination_service_account: Optional[str]
    protocol: Optional[str]
    port: Optional[int]  # None nếu port_unknown — Ý NGHĨA KHÔNG ĐỔI so với v1: đây là
                         # port Tier 1 dùng để khớp với identity_key.port (traffic thật,
                         # tức target_port — mục "Vì sao port phải lấy targetPort" của
                         # CONFIG_EVIDENCE_COLLECTOR.md v2 mục 4.4d). KHÔNG đổi tên/ý
                         # nghĩa field này để `pipeline.py::_pick_config_evidence` không
                         # phải sửa gì (đúng cam kết "Tier 1 không phải sửa logic").
    port_unknown: bool

    config_finding: ConfigFinding
    evidence_source: str  # "env" | "envFrom" | "command" | "args" | "configmap" | ... (xem EvidenceRef.source_kind)
    scope: Scope

    snapshot_id: str
    collected_at: str

    # --- Mới ở v2 (mục 6) — THUẦN METADATA, Tier 1 (pipeline.py/scoring.py/candidates.py)
    # KHÔNG đọc các field dưới đây, chỉ dùng cho traceability/dashboard/đánh giá (mục 9).
    # Mặc định an toàn cho record do baseline regex (v1) sinh ra, vốn không có các khái
    # niệm này. Thêm field ở CUỐI, toàn bộ có default, để không phá vỡ bất kỳ lệnh gọi
    # `ConfigEvidenceRecord(...)` nào đã có (regex_baseline.py, test cũ).
    destination_service_ref: Optional[ServiceRef] = None
    raw_host: Optional[str] = None  # luôn có ở v2 — đích dùng khi chưa phân giải được (FOUND_UNRESOLVED)
    service_port: Optional[int] = None  # port THEO CONFIG khai báo (trước khi map sang targetPort) — chỉ để truy vết
    unresolved_reason: Optional[str] = None  # NO_BACKEND | SERVICE_NOT_FOUND | NO_MATCH | EXTERNAL_UNREGISTERED | ...
    external_kind: Optional[str] = None  # SERVICE_ENTRY | EXTERNAL_NAME | SELECTORLESS_ENDPOINTS | ...
    flags: Tuple[str, ...] = ()  # port_mismatch | port_inferred | dest_not_running | selector_overlap | via_virtualservice
    evidence_refs: Tuple[EvidenceRef, ...] = field(default_factory=tuple)
    extraction_method: str = "regex_baseline"  # "llm" | "regex_baseline"
    prompt_version: Optional[str] = None
    model_id: Optional[str] = None
    run_id: Optional[str] = None

    def pair_key(self) -> Tuple[str, str, str, str]:
        return (
            self.source_namespace,
            self.source_canonical_service,
            self.destination_namespace,
            self.destination_canonical_service,
        )


# ---------------------------------------------------------------------------
# Kết quả đầu ra — TIER1_DESIGN.md mục 10 (+ bookkeeping nội bộ)
# ---------------------------------------------------------------------------

@dataclass
class DependencyEdgeResult:
    """Một dòng evidence hoàn chỉnh cho MỘT identity_key, sau khi đã qua toàn bộ pipeline.

    Các field ở nửa đầu bám sát 1:1 JSON schema mục 10. Nửa sau là các cờ nghiệp vụ
    phục vụ escalation/hysteresis/human-review, không có trong schema gốc nhưng cần
    để pipeline vận hành được và để lưu SQLite (storage.py mục 11 bên TIER2_DESIGN.md
    dùng chung bảng dependency_evidence)."""

    identity_key: IdentityKey
    destination_service_ref: Optional[ServiceRef]

    scope: Scope
    traffic_class: TrafficClass

    count: int
    distinct_days: int
    error_ratio: float
    unstable: bool

    config_finding: Optional[ConfigFinding]

    score: Optional[float]  # None cho DNS (không scoring) hoặc NOT_OBSERVED thuần túy
    dependency_status: DependencyStatus

    snapshot_id: str
    workload_revision: Optional[str] = None

    # --- bookkeeping / quyết định luồng đi tiếp (không thuộc schema mục 10 gốc) ---
    previous_status: Optional[DependencyStatus] = None
    unstable_classification: bool = False  # đổi trạng thái >=3 lần/7 ngày (mục 5b)
    needs_human_review: bool = False  # nhánh NOT_OBSERVED sạch (mục 5) — không escalate AI, không tự enforce
    escalate_to_tier2: bool = False
    escalation_reason: Optional[str] = None
    manual_selector_reason: Optional[str] = None  # lý do NEEDS_MANUAL_SELECTOR (mục 3b)
    dns_auto_allow: bool = False  # mục 7 — DNS auto-allow, tách hẳn khỏi 5-state chính thức
