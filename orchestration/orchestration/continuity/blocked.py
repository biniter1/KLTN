"""Log kết nối bị chặn -> `BlockedConnection` (báo cáo mục 3.10.4).

NetworkPolicy của Kubernetes không tự ghi log kết nối bị chặn, nên nguồn phải là CNI hoặc mesh.
Mỗi định dạng có 1 bộ đọc; đều chuẩn hóa về cùng 1 kiểu:

  - `cilium` : `hubble observe -o json` — verdict DROPPED/DENIED với lý do chặn do policy.
  - `calico` : flow log của Calico — `action: deny`.
  - `ztunnel`: access log ztunnel (dòng xuất từ VictoriaLogs, cùng tên trường với
               `tier1/log_collector.py`) có `error` chứa "policy rejection" (AuthorizationPolicy).
  - `generic`: JSON phẳng tự chuẩn hóa — dùng cho CNI khác (vd kindnet/kube-network-policies)
               sau khi tự đổi tên trường.

`flow_key` = 'ns nguồn|workload nguồn|ns đích|workload đích|giao thức|port' — các lần chặn
cùng kết nối gộp về 1 việc trong hàng đợi review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

FORMATS = ("cilium", "calico", "ztunnel", "generic")


@dataclass(frozen=True)
class BlockedConnection:
    observed_at: str
    reporter: str
    source_namespace: Optional[str] = None
    source_workload: Optional[str] = None
    source_service_account: Optional[str] = None
    source_ip: Optional[str] = None
    destination_namespace: Optional[str] = None
    destination_workload: Optional[str] = None
    destination_ip: Optional[str] = None
    protocol: Optional[str] = None
    port: Optional[int] = None
    detail: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def flow_key(self) -> str:
        parts = (self.source_namespace, self.source_workload or self.source_ip, self.destination_namespace,
                 self.destination_workload or self.destination_ip, self.protocol, self.port)
        return "|".join("" if p is None else str(p) for p in parts)

    @property
    def event_key(self) -> str:
        raw = json.dumps({k: v for k, v in asdict(self).items() if k != "detail"}, sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()


def _int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _workload(endpoint: dict) -> Optional[str]:
    wls = endpoint.get("workloads") or []
    if wls and isinstance(wls[0], dict) and wls[0].get("name"):
        return wls[0]["name"]
    return endpoint.get("pod_name")


def _sa_from_spiffe(identity: Optional[str]) -> Optional[str]:
    if identity and "/sa/" in identity:
        return identity.rsplit("/sa/", 1)[1]
    return None


# ------------------------------------------------------------------ bộ đọc từng định dạng
def parse_cilium(obj: dict) -> Optional[BlockedConnection]:
    flow = obj.get("flow", obj)
    if flow.get("verdict") not in ("DROPPED", "DENIED"):
        return None
    reason = str(flow.get("drop_reason_desc") or flow.get("drop_reason") or "")
    if "POLICY" not in reason.upper():
        return None
    src, dst = flow.get("source") or {}, flow.get("destination") or {}
    ip = flow.get("IP") or {}
    l4 = flow.get("l4") or {}
    proto = next(iter(l4), None)
    port = _int((l4.get(proto) or {}).get("destination_port")) if proto else None
    return BlockedConnection(
        observed_at=flow.get("time", ""), reporter="cilium",
        source_namespace=src.get("namespace"), source_workload=_workload(src), source_ip=ip.get("source"),
        destination_namespace=dst.get("namespace"), destination_workload=_workload(dst),
        destination_ip=ip.get("destination"), protocol=proto, port=port,
        detail={"drop_reason": reason, "direction": flow.get("traffic_direction")},
    )


def parse_calico(obj: dict) -> Optional[BlockedConnection]:
    if str(obj.get("action", "")).lower() != "deny":
        return None
    proto = obj.get("proto")
    return BlockedConnection(
        observed_at=str(obj.get("end_time") or obj.get("start_time") or ""), reporter="calico",
        source_namespace=obj.get("source_namespace"), source_workload=obj.get("source_name_aggr"),
        source_ip=obj.get("source_ip"),
        destination_namespace=obj.get("dest_namespace"), destination_workload=obj.get("dest_name_aggr"),
        destination_ip=obj.get("dest_ip"), protocol=str(proto).upper() if proto else None,
        port=_int(obj.get("dest_port")),
        detail={"policies": obj.get("policies"), "num_flows": obj.get("num_flows"), "reporter": obj.get("reporter")},
    )


def parse_ztunnel(obj: dict) -> Optional[BlockedConnection]:
    error = str(obj.get("error") or "")
    if "policy rejection" not in error.lower():
        return None
    addr = obj.get("dst.hbone_addr") or obj.get("dst.addr") or ""
    host, _, port = addr.rpartition(":")
    return BlockedConnection(
        observed_at=obj.get("_time", ""), reporter="ztunnel",
        source_namespace=obj.get("src.namespace"), source_workload=obj.get("src.workload"),
        source_service_account=_sa_from_spiffe(obj.get("src.identity")), source_ip=obj.get("src.addr"),
        destination_namespace=obj.get("dst.namespace"), destination_workload=obj.get("dst.workload"),
        destination_ip=host or None, protocol="TCP", port=_int(port),
        detail={"error": error},
    )


def parse_generic(obj: dict) -> Optional[BlockedConnection]:
    if not obj.get("time"):
        return None
    return BlockedConnection(
        observed_at=obj["time"], reporter="generic",
        source_namespace=obj.get("source_namespace"), source_workload=obj.get("source_workload"),
        source_service_account=obj.get("source_service_account"), source_ip=obj.get("source_ip"),
        destination_namespace=obj.get("destination_namespace"), destination_workload=obj.get("destination_workload"),
        destination_ip=obj.get("destination_ip"),
        protocol=str(obj["protocol"]).upper() if obj.get("protocol") else None, port=_int(obj.get("port")),
        detail={k: v for k, v in obj.items() if k in ("reason", "policy")},
    )


PARSERS: dict[str, Callable[[dict], Optional[BlockedConnection]]] = {
    "cilium": parse_cilium, "calico": parse_calico, "ztunnel": parse_ztunnel, "generic": parse_generic,
}


def parse_blocked_log(lines: Iterable[str], fmt: str) -> list[BlockedConnection]:
    if fmt not in PARSERS:
        raise ValueError(f"định dạng không hỗ trợ: {fmt!r} — chọn 1 trong {FORMATS}")
    parser = PARSERS[fmt]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            conn = parser(obj)
            if conn is not None:
                out.append(conn)
    return out


_ZTUNNEL_FIELDS = ("_time", "src.namespace", "src.workload", "src.identity", "src.addr",
                   "dst.namespace", "dst.workload", "dst.hbone_addr", "dst.addr", "error")


def build_ztunnel_denied_query(start: datetime, end: datetime, stream_filter: str = "") -> str:
    """LogsQL lấy các kết nối ztunnel từ chối do policy (cùng nguồn VictoriaLogs Tầng 1 đang đọc)."""
    if not stream_filter:
        from tier1.log_collector import ZTUNNEL_STREAM_FILTER as stream_filter
    iso = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return (f'_time:[{iso(start)}, {iso(end)}) {stream_filter} "policy rejection" | unpack_logfmt'
            f" | fields {', '.join(_ZTUNNEL_FIELDS)}")


def collect_ztunnel_denied(client, start: datetime, end: datetime) -> list[BlockedConnection]:
    """`client`: `tier1.log_collector.HttpVictoriaLogsClient` (hoặc FakeLogsClient khi test)."""
    out = []
    for row in client.query(build_ztunnel_denied_query(start, end)):
        conn = parse_ztunnel(row)
        if conn is not None:
            out.append(conn)
    return out
