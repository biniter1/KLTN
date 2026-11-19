"""Ngoại lệ khẩn cấp — phần THUẦN (không I/O): kiểm lời khai báo, sinh NetworkPolicy ngoại lệ,
suy trạng thái hiện hành. Báo cáo mục 3.10.5 / Bảng 3.9; MIGRATION_PLANNER_DESIGN.md mục 2.2.

Trạng thái KHÔNG lưu mà suy ra:
  - đi qua các quyết định của người trên `emergency_exception/<id>` theo thứ tự thời gian:
      EXTEND_EXCEPTION -> hạn mới = payload.new_expires_at
      REVOKE           -> REVOKED         (đã đóng, kết thúc)
      MAKE_PERMANENT   -> MADE_PERMANENT  (đã hợp thức hóa qua GitOps, kết thúc)
  - chưa kết thúc: now >= hạn hiện hành -> EXPIRED, ngược lại ACTIVE.
EXPIRED nằm trong hàng đợi review cho tới khi người quản trị gia hạn / đóng / hợp thức hóa
(không tự gỡ — nguyên tắc "hệ thống không tự rút quyền").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from ..continuity.audit import EXCEPTION_LABEL

DEFAULT_MAX_TTL = timedelta(days=7)   # Bảng 3.9: giá trị khởi tạo
TERMINAL = ("REVOKED", "MADE_PERMANENT")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_NP_PROTOCOL = {"TCP": "TCP", "UDP": "UDP", "SCTP": "SCTP", "HTTP": "TCP", "HTTPS": "TCP", "GRPC": "TCP"}


class ExceptionValidationError(ValueError):
    pass


def parse_time(raw: str) -> datetime:
    t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _req(body: dict, key: str) -> str:
    v = body.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ExceptionValidationError(f"{key} là trường bắt buộc (Bảng 3.9), phải là chuỗi không rỗng")
    return v.strip()


def _selector(body: dict, key: str, default_app: str) -> dict:
    sel = body.get(key)
    if sel is None:
        return {"app": default_app}
    if (not isinstance(sel, dict) or not sel
            or not all(isinstance(k, str) and isinstance(v, str) and _LABEL_VALUE.match(v) for k, v in sel.items())):
        raise ExceptionValidationError(f"{key} phải là map nhãn chuỗi->chuỗi hợp lệ, không rỗng")
    return dict(sel)


def validate_declaration(body: dict, *, now: datetime, max_ttl: timedelta = DEFAULT_MAX_TTL) -> dict:
    """Trả bản ghi đã chuẩn hóa (chưa có exception_id/created_at). Lỗi -> ExceptionValidationError."""
    if not isinstance(body, dict):
        raise ExceptionValidationError("thân request phải là object")
    rec = {k: _req(body, k) for k in ("requested_by", "reason", "ticket", "source_namespace", "source_service",
                                      "destination_namespace", "destination_service", "protocol")}
    for k in ("source_namespace", "destination_namespace"):
        if not _DNS_LABEL.match(rec[k]):
            raise ExceptionValidationError(f"{k} không phải tên namespace hợp lệ")
    rec["protocol"] = rec["protocol"].upper()
    if rec["protocol"] not in _NP_PROTOCOL:
        raise ExceptionValidationError(f"protocol phải là 1 trong {sorted(_NP_PROTOCOL)}")
    port = body.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ExceptionValidationError("port phải là số nguyên 1-65535 (ngoại lệ mở ĐÚNG 1 cổng)")
    rec["port"] = port
    sa = body.get("source_service_account")
    rec["source_service_account"] = sa.strip() if isinstance(sa, str) and sa.strip() else None
    rec["source_pod_selector"] = _selector(body, "source_pod_selector", rec["source_service"])
    rec["destination_pod_selector"] = _selector(body, "destination_pod_selector", rec["destination_service"])
    raw_exp = body.get("expires_at")
    if not isinstance(raw_exp, str) or not raw_exp.strip():
        raise ExceptionValidationError("expires_at là bắt buộc — không có ngoại lệ vô thời hạn")
    try:
        expires = parse_time(raw_exp.strip())
    except ValueError as exc:
        raise ExceptionValidationError("expires_at phải là thời gian ISO 8601") from exc
    check_expiry(expires, now=now, max_ttl=max_ttl, field="expires_at")
    rec["expires_at"] = expires.astimezone(timezone.utc).isoformat()
    flow = body.get("blocked_flow_key")
    rec["blocked_flow_key"] = flow.strip() if isinstance(flow, str) and flow.strip() else None
    return rec


def check_expiry(expires: datetime, *, now: datetime, max_ttl: timedelta, field: str) -> None:
    if expires <= now:
        raise ExceptionValidationError(f"{field} phải ở tương lai")
    if expires - now > max_ttl:
        raise ExceptionValidationError(
            f"{field} vượt hạn tối đa {max_ttl.days} ngày — cần lâu hơn thì hợp thức hóa qua GitOps")


def resource_name(exception_id: str) -> str:
    return f"zt-exc-{exception_id.removeprefix('exc_')}"


def render_policies(exc: dict) -> list[dict]:
    """2 NetworkPolicy cùng tên, cùng nhãn `kltn.zt/exception-id`: ingress ở namespace đích,
    egress ở namespace nguồn (cụm mặc định chặn cả 2 chiều). Công cụ CHỈ sinh YAML; người quản
    trị tự áp dụng (công cụ không ghi vào cụm — mục 3.3)."""
    src_sel = _json(exc["source_pod_selector"])
    dst_sel = _json(exc["destination_pod_selector"])
    ports = [{"protocol": _NP_PROTOCOL[exc["protocol"]], "port": exc["port"]}]
    meta = lambda ns: {  # noqa: E731
        "name": exc["resource_name"], "namespace": ns,
        "labels": {EXCEPTION_LABEL: exc["exception_id"], "kltn.zt/kind": "emergency-exception"},
        "annotations": {"kltn.zt/expires-at": exc["expires_at"], "kltn.zt/requested-by": exc["requested_by"],
                        "kltn.zt/ticket": exc["ticket"], "kltn.zt/reason": exc["reason"]},
    }
    ingress = {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(exc["destination_namespace"]),
        "spec": {"podSelector": {"matchLabels": dst_sel}, "policyTypes": ["Ingress"],
                 "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                     "kubernetes.io/metadata.name": exc["source_namespace"]}},
                     "podSelector": {"matchLabels": src_sel}}], "ports": ports}]},
    }
    egress = {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(exc["source_namespace"]),
        "spec": {"podSelector": {"matchLabels": src_sel}, "policyTypes": ["Egress"],
                 "egress": [{"to": [{"namespaceSelector": {"matchLabels": {
                     "kubernetes.io/metadata.name": exc["destination_namespace"]}},
                     "podSelector": {"matchLabels": dst_sel}}], "ports": ports}]},
    }
    return [ingress, egress]


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


@dataclass(frozen=True)
class ExceptionState:
    status: str                   # ACTIVE | EXPIRED | REVOKED | MADE_PERMANENT
    effective_expires_at: str
    last_decision: Optional[dict]


def effective_state(exc: dict, decisions: Iterable[dict], now: datetime) -> ExceptionState:
    """`decisions`: quyết định trên `emergency_exception/<id>`, đã sắp theo thời gian tăng dần."""
    expires = parse_time(exc["expires_at"])
    status: Optional[str] = None
    last = None
    for d in decisions:
        last = d
        if d["action"] == "EXTEND_EXCEPTION":
            payload = _json(d.get("payload")) or {}
            expires = parse_time(payload["new_expires_at"])
            status = None
        elif d["action"] == "REVOKE":
            status = "REVOKED"
        elif d["action"] == "MAKE_PERMANENT":
            status = "MADE_PERMANENT"
    if status is None:
        status = "EXPIRED" if now >= expires else "ACTIVE"
    return ExceptionState(status, expires.astimezone(timezone.utc).isoformat(), last)


def covers_change(exc: dict, state: ExceptionState, change: dict) -> bool:
    """1 thay đổi trong Audit Log quy được về ngoại lệ này nếu: đúng policy của ngoại lệ (tên +
    namespace nguồn/đích, hoặc nhãn exception-id) và xảy ra trong thời gian ngoại lệ còn hiệu
    lực. Xóa policy ngoại lệ (đóng lại) luôn coi là hợp lệ — đó là hành động mong muốn."""
    same_ns = change["resource_namespace"] in (exc["source_namespace"], exc["destination_namespace"])
    named = change["resource_kind"] == exc["resource_kind"] and change["resource_name"] == exc["resource_name"]
    labelled = change.get("exception_label") == exc["exception_id"]
    if not same_ns or not (named or labelled):
        return False
    if change["change_type"] == "DELETE":
        return True
    try:
        at = parse_time(change["changed_at"])
    except ValueError:
        return False
    # cho trễ 10 phút: admin có thể áp policy trước rồi mới khai báo trên dashboard
    start = parse_time(exc["created_at"]) - timedelta(minutes=10)
    end = parse_time(state.effective_expires_at)
    return start <= at <= end and state.status != "REVOKED"
