"""Kiểu dữ liệu dùng chung toàn bộ Tier 2.

Enum/dataclass ở đây bám sát tên trường trong TIER2_DESIGN.md để code và
tài liệu thiết kế đối chiếu 1-1 được (xem README phần "Ánh xạ thiết kế -> code").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class HypothesisStatus(str, Enum):
    """5 trạng thái hypothesis, thuần rule-based (mục 7.3)."""

    PROPOSED = "PROPOSED"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    CONFLICTED = "CONFLICTED"
    INSUFFICIENT = "INSUFFICIENT"


class FinalStatus(str, Enum):
    """4 trạng thái cuối của cạnh, trả về Batch Orchestrator (mục 0, 7.4)."""

    CONFIDENT = "CONFIDENT"
    NEEDS_MONITORING = "NEEDS_MONITORING"
    INSUFFICIENT_BASIS = "INSUFFICIENT_BASIS"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class Stance(str, Enum):
    SUPPORT = "support"
    CONTRADICT = "contradict"


class ToolRowStatus(str, Enum):
    OK = "OK"
    ERROR = "ERROR"


class ModelAction(str, Enum):
    CALL_TOOL = "call_tool"
    CONCLUDE = "conclude"


class StoppedBy(str, Enum):
    MODEL = "model"
    OBSERVER_POLICY = "observer_policy"


class ToolName(str, Enum):
    """Enum 4 giá trị cố định (mục 10, whitelist tool name)."""

    READ_SOURCE_CODE = "read_source_code"
    READ_PR_DESCRIPTIONS = "read_pr_descriptions"
    READ_INTERNAL_DOCS = "read_internal_docs"
    READ_EXTENDED_TRAFFIC_WINDOW = "read_extended_traffic_window"


# Tool duy nhất trả về bằng chứng "cấu trúc" (số liệu traffic của Tier 1),
# 3 tool còn lại trả text phi cấu trúc. Dùng cho corroboration rule (mục 7.4).
STRUCTURED_TOOLS = frozenset({ToolName.READ_EXTENDED_TRAFFIC_WINDOW.value})

# Thứ hạng độ tin cậy ORDINAL (mục 7.5) — số nhỏ hơn = đáng tin hơn.
EVIDENCE_RELIABILITY_RANK = {
    ToolName.READ_EXTENDED_TRAFFIC_WINDOW.value: 0,
    ToolName.READ_PR_DESCRIPTIONS.value: 1,
    ToolName.READ_SOURCE_CODE.value: 2,
    ToolName.READ_INTERNAL_DOCS.value: 3,
}


# ---------------------------------------------------------------------------
# Escalation input (TIER1_DESIGN.md mục 10) — xem tier2_agent.escalation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DestinationServiceRef:
    service_name: str
    service_namespace: str


# ---------------------------------------------------------------------------
# Hypothesis & Evidence
# ---------------------------------------------------------------------------


@dataclass
class Hypothesis:
    id: str
    statement: str
    status: HypothesisStatus = HypothesisStatus.PROPOSED


@dataclass
class EvidenceRow:
    """1 dòng `trace_log` (mục 11.1)."""

    evidence_id: str
    run_id: str
    iteration: int
    hypothesis_id: str
    tool_name: str
    tool_input: dict
    tool_output: str
    stance: Optional[str] = None
    stale: bool = False
    status: str = ToolRowStatus.OK.value
    error_message: Optional[str] = None
    injection_flagged: bool = False
    sanitized_flagged: bool = False
    truncated: bool = False
    pr_number: Optional[str] = None


@dataclass
class HypothesisView:
    """Hypothesis đã nhóm evidence — output của Context Manager.build (Builder)."""

    id: str
    statement: str
    status: HypothesisStatus
    evidence: list = field(default_factory=list)  # list[EvidenceRow]

    @property
    def has_structured_support(self) -> bool:
        return any(
            e.tool_name in STRUCTURED_TOOLS and e.stance == Stance.SUPPORT.value
            for e in self.evidence
            if e.status == ToolRowStatus.OK.value
        )


# ---------------------------------------------------------------------------
# Model Output (mục 6)
# ---------------------------------------------------------------------------


@dataclass
class StanceUpdate:
    evidence_id: str
    stance: str


@dataclass
class NewHypothesis:
    id: str
    statement: str


@dataclass
class ToolCallRequest:
    tool_name: str
    tool_input: dict
    hypothesis_id: str


@dataclass
class ModelOutput:
    reasoning: str
    action: str
    stance_updates: list = field(default_factory=list)  # list[StanceUpdate]
    new_hypotheses: list = field(default_factory=list)  # list[NewHypothesis]
    tool_calls: list = field(default_factory=list)  # list[ToolCallRequest]


# ---------------------------------------------------------------------------
# State (mục 5)
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    run_id: str
    edge_identity_key: str
    session_started_at: float  # epoch giây — LƯU THẬT
    manual_abort_flag: bool = False  # LƯU THẬT — dashboard bấm nút ghi cờ này
    phase: str = "RUNNING"  # LƯU THẬT — RUNNING | CONCLUDED


@dataclass
class LiveCounts:
    """3 field TÍNH LẠI mỗi lần cần, không lưu (mục 5)."""

    iteration_count: int
    tools_called: set
    hypotheses_opened_count: int
    elapsed_ms: float


# ---------------------------------------------------------------------------
# Kết quả mỗi vòng / kết quả cuối (mục 8.2, 11.2, 11.3)
# ---------------------------------------------------------------------------


@dataclass
class ObserverDecision:
    stop: bool
    stopped_by: Optional[str] = None  # StoppedBy
    stop_reason: Optional[str] = None


@dataclass
class RoundResult:
    """"Valid result" (đang tiếp tục) hoặc "Final Result" (đã dừng) — cùng 1 shape
    (mục 8.2: Validation + Result Formatter chạy MỌI vòng)."""

    run_id: str
    iteration: int
    hypotheses: list  # list[HypothesisView]
    final_status: str  # FinalStatus
    original_agent_status: str  # FinalStatus, trước khi bị corroboration hạ cấp
    action: str
    stopped: bool
    stopped_by: Optional[str]
    stop_reason: Optional[str]
    persisted: bool  # True chỉ khi đây là Final Result
    reasoning: str = ""
