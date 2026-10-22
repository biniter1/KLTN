"""Danh mục lý do cần con người + luật hành động (DASHBOARD_DESIGN.md mục 2) — THUẦN, không I/O.

Mỗi `reason_code` có:
  - `target_kind`: loại đối tượng mà quyết định trỏ tới (cạnh dependency / tài nguyên K8s /
    phiên Tier 2 / ngoại lệ khẩn cấp) -> quyết định shape `target_ref` hợp lệ.
  - `actions`: tập action hợp lệ — ánh xạ từ cột "Hành động con người cần làm" của bảng mục 2.
  - `source`: module sinh ra loại việc này. 5 loại cuối (Policy Impact Validator, Continuity
    Monitor) CHƯA có module sinh — schema + POST /decisions đã nhận đủ, nhưng
    /review-queue hiện chỉ tự sinh 6 loại đầu (xem README "Phạm vi").
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..db import ACTIONS, REASON_CODES


@dataclass(frozen=True)
class ReasonSpec:
    target_kind: str            # "edge" | "resource" | "agent_session" | "exception"
    actions: frozenset
    source: str
    urgency: str                # "high" | "medium" | "low" — thứ tự hiển thị hàng đợi
    generated_now: bool         # /review-queue đã tự sinh được loại này chưa


REASONS: dict[str, ReasonSpec] = {
    "CLEAN_CONFIG_MATCH": ReasonSpec("edge", frozenset({"APPROVE", "REJECT"}),
                                     "Tier 1 (NOT_OBSERVED + config sạch)", "medium", True),
    "NEEDS_MANUAL_SELECTOR": ReasonSpec("edge", frozenset({"SUBMIT_SELECTOR", "REJECT"}),
                                        "Tier 1 mục 3b", "medium", True),
    "HIGH_BLAST_RADIUS": ReasonSpec("edge", frozenset({"APPROVE", "REJECT"}),
                                    "Migration Planner mục 1.5 (đích nhạy cảm)", "high", True),
    "CONFLICTING_EVIDENCE": ReasonSpec("edge", frozenset({"APPROVE", "REJECT"}),
                                       "Tier 2 CONFLICTING_EVIDENCE", "high", True),
    "AGENT_INSUFFICIENT_BASIS": ReasonSpec("edge", frozenset({"APPROVE", "REJECT"}),
                                           "Tier 2 NEEDS_MONITORING / INSUFFICIENT_BASIS", "low", True),
    # NOTE (điểm mở): mục 2 chỉ ghi "Xử lý vận hành (tool hỏng)", enum action không có
    # "ACKNOWLEDGE" -> dùng APPROVE với nghĩa "đã xử lý xong", BẮT BUỘC kèm payload.note.
    "TOOL_ADMIN_ATTENTION": ReasonSpec("agent_session", frozenset({"APPROVE"}),
                                       "Tier 2 brain.py (lỗi tool liên tiếp)", "high", True),
    "POLICY_WOULD_BREAK": ReasonSpec("resource", frozenset({"OVERRIDE_WITH_JUSTIFICATION", "REJECT"}),
                                     "Policy Impact Validator", "high", False),
    "POLICY_UNSUPPORTED_BY_EVIDENCE": ReasonSpec(
        "resource", frozenset({"APPROVE", "REJECT", "OVERRIDE_WITH_JUSTIFICATION"}),
        "Policy Impact Validator", "medium", False),
    "UNATTRIBUTED_CONFIG_CHANGE": ReasonSpec("resource", frozenset({"ATTRIBUTE", "REVOKE", "APPROVE"}),
                                             "Continuity Monitor mục 2.5", "high", False),
    "EMERGENCY_EXCEPTION_EXPIRED": ReasonSpec(
        "exception", frozenset({"EXTEND_EXCEPTION", "REVOKE", "MAKE_PERMANENT"}),
        "Continuity Monitor mục 2.2", "high", False),
    "STALE_PERMISSION": ReasonSpec("edge", frozenset({"REVOKE", "APPROVE"}),
                                   "Continuity Monitor mục 2.8", "low", False),
    # Cổng port ở cửa ra (chốt 22/10/2026): Tầng 1 không chặn cạnh thiếu/mâu thuẫn port —
    # nó vẫn được chấm điểm; chỉ bước sinh YAML cần port nên chặn ở đây, cạnh khác đi tiếp.
    "NEEDS_MANUAL_PORT": ReasonSpec("edge", frozenset({"SUBMIT_PORT", "REJECT"}),
                                    "Migration Planner (cạnh kết luận chưa xác định được port)", "medium", True),
    "PORT_CONFLICT": ReasonSpec("edge", frozenset({"SUBMIT_PORT", "REJECT"}),
                                "Migration Planner (log thấy nhiều port / port suy ra lệch config)", "medium", True),
}

assert set(REASONS) == set(REASON_CODES), "REASONS phải phủ đúng mọi reason_code của db.py"
assert all(spec.actions <= set(ACTIONS) for spec in REASONS.values())

URGENCY_ORDER = {"high": 0, "medium": 1, "low": 2}

TIER2_REVIEW_STATUSES = {
    "CONFLICTING_EVIDENCE": "CONFLICTING_EVIDENCE",
    "NEEDS_MONITORING": "AGENT_INSUFFICIENT_BASIS",
    "INSUFFICIENT_BASIS": "AGENT_INSUFFICIENT_BASIS",
}


class DecisionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedTarget:
    identity_key: Optional[str]
    run_id: Optional[str]
    resource: Optional[str]


def _require_str(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DecisionValidationError(f"{where}.{key} phải là chuỗi không rỗng")
    return value.strip()


def normalize_target(reason_code: str, target_ref: dict) -> NormalizedTarget:
    """Kiểm shape `target_ref` theo loại đối tượng của reason_code (mục 2: "identity_key,
    hoặc resource_kind+namespace+name, tuỳ loại") và tách ra cột để JOIN/lọc."""
    if not isinstance(target_ref, dict):
        raise DecisionValidationError("target_ref phải là object")
    kind = REASONS[reason_code].target_kind
    if kind == "edge":
        identity_key = _require_str(target_ref, "identity_key", "target_ref")
        if identity_key.count("|") != 6:
            raise DecisionValidationError("target_ref.identity_key phải có dạng 7 phần 'ns|svc|sa|ns|svc|proto|port'")
        run_id = target_ref.get("run_id")
        if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
            raise DecisionValidationError("target_ref.run_id nếu có phải là chuỗi không rỗng")
        return NormalizedTarget(identity_key, run_id.strip() if run_id else None, None)
    if kind == "resource":
        parts = [_require_str(target_ref, k, "target_ref") for k in ("resource_kind", "namespace", "name")]
        return NormalizedTarget(target_ref.get("identity_key"), target_ref.get("run_id"), "/".join(parts))
    if kind == "agent_session":
        session_id = _require_str(target_ref, "tier2_session_id", "target_ref")
        return NormalizedTarget(None, None, f"agent_session/{session_id}")
    exception_id = _require_str(target_ref, "exception_id", "target_ref")
    return NormalizedTarget(None, None, f"emergency_exception/{exception_id}")


def validate_payload(action: str, payload: dict, *, now: Optional[datetime] = None,
                     reason_code: Optional[str] = None) -> dict:
    """Payload bắt buộc theo action (mục 2: "payload tuỳ action — vd selector YAML, hạn mới,
    ghi chú lý do ghi đè"). Trả payload đã chuẩn hoá."""
    if not isinstance(payload, dict):
        raise DecisionValidationError("payload phải là object")
    payload = dict(payload)
    now = now or datetime.now(timezone.utc)

    if action == "SUBMIT_SELECTOR":
        pod_selector = payload.get("pod_selector")
        ip_block = payload.get("ip_block")
        if not pod_selector and not ip_block:
            raise DecisionValidationError("SUBMIT_SELECTOR cần payload.pod_selector (matchLabels) hoặc payload.ip_block (CIDR)")
        if pod_selector is not None:
            if (not isinstance(pod_selector, dict) or not pod_selector
                    or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in pod_selector.items())):
                raise DecisionValidationError("payload.pod_selector phải là map nhãn chuỗi->chuỗi, không rỗng")
        if ip_block is not None:
            try:
                ipaddress.ip_network(str(ip_block), strict=False)
            except ValueError as exc:
                raise DecisionValidationError(f"payload.ip_block không phải CIDR hợp lệ: {exc}") from exc
    elif action == "EXTEND_EXCEPTION":
        raw = _require_str(payload, "new_expires_at", "payload")
        try:
            expires = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise DecisionValidationError("payload.new_expires_at phải là thời gian ISO 8601") from exc
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            raise DecisionValidationError("payload.new_expires_at phải ở tương lai")
    elif action == "OVERRIDE_WITH_JUSTIFICATION":
        justification = _require_str(payload, "justification", "payload")
        if len(justification) < 10:
            raise DecisionValidationError("payload.justification quá ngắn — ghi đè phải kèm lý do cụ thể (>= 10 ký tự)")
    elif action == "ATTRIBUTE":
        _require_str(payload, "owner", "payload")
    elif action == "SUBMIT_PORT":
        port = payload.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise DecisionValidationError("SUBMIT_PORT cần payload.port là số nguyên 1..65535 (targetPort của pod đích)")

    if reason_code == "TOOL_ADMIN_ATTENTION":
        _require_str(payload, "note", "payload")
    return payload


def validate_decision(reason_code: str, action: str, target_ref: dict, payload: dict,
                      *, now: Optional[datetime] = None) -> tuple[NormalizedTarget, dict]:
    if reason_code not in REASONS:
        raise DecisionValidationError(f"reason_code không hợp lệ: {reason_code!r}")
    if action not in ACTIONS:
        raise DecisionValidationError(f"action không hợp lệ: {action!r}")
    spec = REASONS[reason_code]
    if action not in spec.actions:
        raise DecisionValidationError(
            f"action {action} không áp dụng cho {reason_code} — hợp lệ: {sorted(spec.actions)}")
    target = normalize_target(reason_code, target_ref)
    return target, validate_payload(action, payload, now=now, reason_code=reason_code)


def edge_review_reasons(row: dict, sensitive_namespaces: frozenset) -> list[str]:
    """Từ 1 dòng `dependency_current_state` suy ra loại việc cần người (sơ đồ mục 1).
    DNS auto-allow (mục 7 TIER1) không bao giờ vào hàng đợi."""
    if row.get("dns_auto_allow"):
        return []
    status = row.get("dependency_status")
    tier2 = row.get("tier2_status")
    reasons: list[str] = []
    if status == "NEEDS_MANUAL_SELECTOR":
        reasons.append("NEEDS_MANUAL_SELECTOR")
    if status == "NOT_OBSERVED" and row.get("config_finding") in ("FOUND_STRUCTURAL", "FOUND_EMBEDDED"):
        reasons.append("CLEAN_CONFIG_MATCH")
    if tier2 in TIER2_REVIEW_STATUSES:
        reasons.append(TIER2_REVIEW_STATUSES[tier2])
    concluded_strong = status == "OBSERVED_STRONG" or tier2 == "CONFIDENT"
    sensitive = row.get("scope") == "EXTERNAL" or row.get("destination_namespace") in sensitive_namespaces
    if concluded_strong and sensitive:
        reasons.append("HIGH_BLAST_RADIUS")
    port_reason = edge_port_reason(row)
    if port_reason and (concluded_strong or status == "NEEDS_MANUAL_SELECTOR" or row.get("auto_allow_reason")):
        reasons.append(port_reason)
    return reasons


_INFERRED_PORT_SOURCES = ("inferred_single_port", "inferred_protocol_match")


def _row_flags(row: dict) -> set:
    raw = row.get("flags")
    if isinstance(raw, (list, tuple, set)):
        return set(raw)
    if isinstance(raw, str) and raw:
        try:
            return set(json.loads(raw))
        except ValueError:
            return set()
    return set()


def edge_port_reason(row: dict) -> Optional[str]:
    """Cổng port ở cửa ra (chốt 22/10/2026). Chỉ xét cạnh SẮP được sinh rule (caller lọc).
      - PORT_CONFLICT: log thấy nhiều port (`multiple_ports_seen`), hoặc port SUY RA từ Service
        lệch với port trong config (`config_port_differs`). Port THẤY ĐƯỢC lệch config không
        tính — config lệch đó đã đi nhánh NOT_OBSERVED riêng (config dư).
      - NEEDS_MANUAL_PORT: không xác định được port.
    `inferred_protocol_match` không mâu thuẫn thì cho qua (port_source vẫn hiện trên dashboard)."""
    flags = _row_flags(row)
    if "multiple_ports_seen" in flags:
        return "PORT_CONFLICT"
    if "config_port_differs" in flags and row.get("port_source") in _INFERRED_PORT_SOURCES:
        return "PORT_CONFLICT"
    if row.get("port") is None:
        return "NEEDS_MANUAL_PORT"
    return None


def max_consecutive_errors(trace_rows: list[dict]) -> dict:
    """tool_name -> (độ dài chuỗi ERROR liên tiếp dài nhất, error_message cuối). Dùng phát
    hiện TOOL_ADMIN_ATTENTION từ `trace_log` (brain.py chỉ ghi cờ này dạng chuỗi notice)."""
    streaks: dict = {}
    current: dict = {}
    for r in trace_rows:
        tool = r["tool_name"]
        if r["status"] == "ERROR":
            current[tool] = current.get(tool, 0) + 1
            best, _msg = streaks.get(tool, (0, None))
            if current[tool] >= best:
                streaks[tool] = (current[tool], r.get("error_message"))
        else:
            current[tool] = 0
    return streaks
