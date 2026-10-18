"""
Đọc access log của ztunnel từ VictoriaLogs — nguồn bằng chứng runtime thứ 2 của Tầng 1
(bổ sung cho bộ đếm Prometheus/VictoriaMetrics), tích hợp stack của repo LOG_METRIC
(vm-vl-ha-lab: vlagent DaemonSet gửi đôi vào vlogs-a/vlogs-b, đọc qua vmauth
`/select/logsql/*`, `first_available` A -> B).

Vì sao cần log (đối chiếu dữ liệu thật 08/10/2026):
  - `istio_requests_total` KHÔNG có nhãn `destination_port` -> mọi series HTTP/gRPC có
    port = None. Mỗi dòng log "connection complete" của ztunnel ghi `dst.hbone_addr`
    (hoặc `dst.addr`) = IP:PORT pod đích THẬT -> port quan sát được (port_source =
    observed_log), không phải suy từ Service.
  - Cạnh TCP thuần (redis, postgres...) đi qua ztunnel kể cả khi không qua waypoint.

Mỗi kết nối được 2 ztunnel ghi (outbound ở node nguồn, inbound ở node đích) -> chỉ lấy
`direction=inbound` để đếm 1 lần (tương đương reporter=destination). Log đếm KẾT NỐI, không
đếm request -> count của series log không cộng với count request của metric (xem
`merge_log_series`).

Truy vấn dùng LogsQL `stats` theo ngày ngay trên VictoriaLogs (không kéo log thô về), nên
khối lượng trả về chỉ bằng số tổ hợp (nguồn, đích, IP:port đích, ngày).

NOTE — cần kiểm chứng trên VM (không có VictoriaLogs/ztunnel trong sandbox soạn code):
  1. Bộ lọc luồng log mặc định `ZTUNNEL_STREAM_FILTER` (pod ztunnel ở istio-system, vlagent
     gắn field `kubernetes.pod_namespace` / `kubernetes.pod_name` — docs/04-luong-logs.md
     của LOG_METRIC). Đổi qua tham số `stream_filter` nếu tên field khác.
  2. ztunnel ghi log dạng logfmt (mặc định) hoặc JSON (`logAsJson`) — query luôn chạy
     `unpack_logfmt`; với log JSON vlagent đã tách field sẵn, pipe này không làm hỏng gì.
  3. Tên field theo log ztunnel 1.2x–1.3x: src.namespace, src.workload, src.identity,
     dst.namespace, dst.workload, dst.service, dst.identity, dst.addr, dst.hbone_addr,
     direction. Parser Python chịu được thiếu field (bỏ dòng thiếu đích).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from .schema import ServiceRef, TrafficSeries

# Cổng HBONE của ztunnel — dst.addr trỏ cổng này khi traffic đi qua HBONE, không phải
# port ứng dụng; khi đó phải lấy port từ dst.hbone_addr.
HBONE_PORT = 15008

ZTUNNEL_STREAM_FILTER = 'kubernetes.pod_namespace:="istio-system" kubernetes.pod_name:ztunnel*'

_GROUP_FIELDS = (
    "src.namespace", "src.workload", "src.identity",
    "dst.namespace", "dst.workload", "dst.service", "dst.identity",
    "dst.hbone_addr", "dst.addr",
)


class LogsClient(Protocol):
    def query(self, logsql: str) -> list[dict]:
        """Trả về danh sách object (mỗi dòng kết quả LogsQL là 1 JSON object)."""
        ...


class FakeLogsClient:
    """Cho test: trả `rows` cố định (hoặc gọi `matcher(logsql)`), ghi lại các query đã nhận."""

    def __init__(self, rows: list[dict] | None = None, matcher=None):
        self._rows = rows or []
        self._matcher = matcher
        self.queries: list[str] = []

    def query(self, logsql: str) -> list[dict]:
        self.queries.append(logsql)
        if self._matcher is not None:
            return self._matcher(logsql)
        return list(self._rows)


class HttpVictoriaLogsClient:
    """Gọi `{base_url}/select/logsql/query` — base_url là vmauth (vd http://vmauth:8427)
    hoặc thẳng 1 instance VictoriaLogs (http://vlogs-a:9428). Kết quả là JSON lines."""

    def __init__(self, base_url: str, timeout_seconds: float = 60.0, headers: Optional[dict] = None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._headers = headers or {}

    def query(self, logsql: str) -> list[dict]:  # pragma: no cover - cần VictoriaLogs thật
        import requests

        resp = requests.post(
            f"{self._base_url}/select/logsql/query",
            data={"query": logsql},
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_ztunnel_flow_query(
    start: datetime,
    end: datetime,
    namespaces: Optional[list[str]] = None,
    stream_filter: str = ZTUNNEL_STREAM_FILTER,
) -> str:
    """LogsQL: các kết nối inbound đã đóng, đếm theo (nguồn, đích, IP:port đích, ngày)."""
    ns_filter = ""
    if namespaces:
        ns_filter = " | filter " + " or ".join(f'dst.namespace:="{ns}"' for ns in namespaces)
    return (
        f'_time:[{_iso(start)}, {_iso(end)}) {stream_filter} "connection complete"'
        " | unpack_logfmt"
        ' | filter direction:="inbound"'
        f"{ns_filter}"
        f" | stats by (_time:1d, {', '.join(_GROUP_FIELDS)}) count() as hits"
    )


def _sa_from_identity(identity: Optional[str]) -> Optional[str]:
    if not identity or "/sa/" not in identity:
        return None
    return identity.rsplit("/sa/", 1)[-1].strip('"')


def _port_of(addr: Optional[str]) -> Optional[int]:
    if not addr or ":" not in addr:
        return None
    try:
        return int(addr.rsplit(":", 1)[-1].strip('"]'))
    except ValueError:
        return None


def _service_ref(fqdn: Optional[str]) -> Optional[ServiceRef]:
    """"cartservice.shared-eval.svc.cluster.local" -> ServiceRef(cartservice, shared-eval)."""
    if not fqdn:
        return None
    parts = fqdn.strip('"').split(".")
    if len(parts) >= 3 and parts[2] == "svc":
        return ServiceRef(parts[0], parts[1])
    return None


def _clean(v) -> str:
    return str(v).strip('"') if v is not None else ""


@dataclass
class _LogAcc:
    src_ns: str
    src_workload: str
    src_sa: str
    dst_ns: str
    dst_workload: str
    dst_sa: Optional[str]
    service_ref: Optional[ServiceRef]
    service_host: Optional[str]
    port: int
    count: int = 0
    days: dict[str, int] = field(default_factory=dict)


@dataclass
class LogCollectionResult:
    series: list[TrafficSeries]
    daily_counts: list[dict[str, int]]
    rows_read: int = 0
    rows_skipped: int = 0


def collect_ztunnel_flows(
    client: LogsClient,
    window_days: int = 7,
    namespaces: Optional[list[str]] = None,
    now: Optional[datetime] = None,
    workload_canonical: Optional[dict[tuple[str, str], str]] = None,
    stream_filter: str = ZTUNNEL_STREAM_FILTER,
) -> LogCollectionResult:
    """Đọc log ztunnel trong cửa sổ `window_days` ngày (tính tới hết ngày `now`) và dựng
    `TrafficSeries` protocol TCP, port quan sát được, `evidence_source = "logs"`.

    `workload_canonical`: (namespace, tên workload) -> canonical service (từ
    `cluster_context.build_cluster_context`) — log ghi tên WORKLOAD, metric ghi canonical;
    thiếu thì dùng tên workload."""
    now = now or datetime.now(timezone.utc)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=window_days)
    rows = client.query(build_ztunnel_flow_query(start, end, namespaces, stream_filter))
    canon = workload_canonical or {}

    accs: dict[tuple, _LogAcc] = {}
    skipped = 0
    for row in rows:
        dst_ns = _clean(row.get("dst.namespace"))
        dst_workload = _clean(row.get("dst.workload"))
        src_ns = _clean(row.get("src.namespace"))
        src_workload = _clean(row.get("src.workload"))
        port = None
        hbone = _clean(row.get("dst.hbone_addr"))
        if hbone:
            port = _port_of(hbone)
        if port is None:
            p = _port_of(_clean(row.get("dst.addr")))
            port = p if p != HBONE_PORT else None
        if not (dst_ns and dst_workload and src_ns and src_workload) or port is None:
            skipped += 1
            continue
        src_sa = _sa_from_identity(_clean(row.get("src.identity"))) or "unknown"
        key = (src_ns, src_workload, src_sa, dst_ns, dst_workload, port)
        acc = accs.get(key)
        if acc is None:
            host = _clean(row.get("dst.service")) or None
            acc = _LogAcc(
                src_ns=src_ns, src_workload=src_workload, src_sa=src_sa,
                dst_ns=dst_ns, dst_workload=dst_workload,
                dst_sa=_sa_from_identity(_clean(row.get("dst.identity"))),
                service_ref=_service_ref(host), service_host=host, port=port,
            )
            accs[key] = acc
        try:
            hits = int(float(row.get("hits", 0)))
        except (TypeError, ValueError):
            hits = 0
        day = _clean(row.get("_time"))[:10]
        acc.count += hits
        if day and hits > 0:
            acc.days[day] = acc.days.get(day, 0) + hits

    series: list[TrafficSeries] = []
    daily: list[dict[str, int]] = []
    today = now.date()
    for acc in accs.values():
        if acc.count <= 0:
            continue
        days = sorted(acc.days)
        last_seen = (today - datetime.fromisoformat(days[-1]).date()).days if days else 9999.0
        series.append(TrafficSeries(
            source_namespace=acc.src_ns,
            source_canonical_service=canon.get((acc.src_ns, acc.src_workload), acc.src_workload),
            source_service_account=acc.src_sa,
            source_workload_namespace=acc.src_ns,
            destination_namespace=acc.dst_ns,
            destination_canonical_service=canon.get((acc.dst_ns, acc.dst_workload), acc.dst_workload),
            destination_service_account=acc.dst_sa,
            destination_service_ref=acc.service_ref,
            protocol="TCP",
            port=acc.port,
            path=None,
            count=acc.count,
            distinct_days=len(days),
            last_seen_days_ago=float(max(last_seen, 0)),
            port_source="observed_log",
            traffic_days=days,
            destination_service_host=acc.service_host,
            evidence_source="logs",
        ))
        daily.append(dict(sorted(acc.days.items())))
    return LogCollectionResult(series=series, daily_counts=daily, rows_read=len(rows), rows_skipped=skipped)


def _edge5(s: TrafficSeries) -> tuple:
    return (s.source_namespace, s.source_canonical_service, s.source_service_account,
            s.destination_namespace, s.destination_canonical_service)


def merge_log_series(
    metric_series: list[TrafficSeries],
    log_series: list[TrafficSeries],
    log_daily: Optional[list[dict[str, int]]] = None,
) -> tuple[list[TrafficSeries], list[dict[str, int]]]:
    """Gộp 2 nguồn (TIER1_DESIGN.md mục 2.1b):

    1. Series metric chưa có port + log thấy ĐÚNG 1 port cho cùng (nguồn, SA, đích) ->
       gán port đó, `port_source = observed_log`. Log thấy nhiều port -> giữ None, cờ
       `multiple_ports_seen` (để suy từ Service spec ở bước sau, hoặc người duyệt chọn).
    2. Series log của cặp ĐÃ có series metric -> bỏ (cùng kết nối, đã được đếm theo
       request); series log của cặp CHƯA có metric -> giữ, là cạnh TCP mới.

    Trả về (series log được giữ, daily_counts tương ứng)."""
    log_daily = log_daily if log_daily is not None else [{} for _ in log_series]
    ports_by_edge: dict[tuple, set[int]] = {}
    for s in log_series:
        ports_by_edge.setdefault(_edge5(s), set()).add(s.port)

    metric_edges = set()
    for s in metric_series:
        metric_edges.add(_edge5(s))
        if s.port is not None:
            continue
        ports = ports_by_edge.get(_edge5(s))
        if not ports:
            continue
        if len(ports) == 1:
            s.port = next(iter(ports))
            s.port_source = "observed_log"
        else:
            s.add_flag("multiple_ports_seen")

    kept, kept_daily = [], []
    for s, d in zip(log_series, log_daily):
        if _edge5(s) not in metric_edges:
            kept.append(s)
            kept_daily.append(d)
    return kept, kept_daily
