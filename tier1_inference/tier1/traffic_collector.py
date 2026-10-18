"""
Traffic Collection Pipeline — TRAFFIC_COLLECTION_PIPELINE.md mục 1-6 (cập nhật 15/10/2026).

Cập nhật 15/10/2026 (TIER1_DESIGN.md mục 2.1, TIER1_CHANGE_PLAN.md giai đoạn B):
  - Nguồn metrics: Prometheus HOẶC VictoriaMetrics (stack LOG_METRIC — đọc qua vmauth
    `/select/0/prometheus`, cùng HTTP API Prometheus). `VictoriaMetricsClient` từ chối kết
    quả `isPartial` (thiếu vmstorage -> thiếu cạnh -> policy chặn nhầm) trừ khi cho phép.
  - Nguồn logs (tùy chọn): access log ztunnel từ VictoriaLogs (`log_collector.py`) -> port
    quan sát được + cạnh TCP không qua waypoint.
  - Port vắng -> None (KHÔNG còn 0); `cluster_context.annotate_series` suy port từ Service.
  - Giữ TẬP NGÀY có traffic (`traffic_days`) thay vì chỉ đếm; số request theo ngày trả ra
    `CollectionResult.daily_counts` (bảng traffic_daily_counts / ablation hysteresis).
  - SỬA LỖI: series TCP trước đây không có query theo ngày -> distinct_days = 0,
    last_seen = 9999 -> cạnh TCP luôn bị chấm điểm thấp. Nay TCP có 7 query theo ngày.
  - Kiểm scrape gap trên CẢ cửa sổ (`min_over_time(up[Kd]) == 0`) cho ztunnel/waypoint,
    job mặc định `kubernetes-pods` (job `istio-proxy` không tồn tại trong cluster thật).
  - Lỗi gRPC đếm thêm theo `grpc_response_status != 0` (gRPC lỗi vẫn trả HTTP 200).

Thu thập traffic thật qua Envoy sidecar (Istio) + Prometheus, KHÔNG đọc log chi tiết
từng request — chỉ đọc bộ đếm (counter metric) đã gắn nhãn theo danh tính service.
Thu thập song song 3 loại (mục 1): HTTP/gRPC (`istio_requests_total`), TCP
(`istio_tcp_connections_opened_total`), DNS (xử lý riêng ở TIER1_DESIGN.md mục 7,
KHÔNG thuộc phạm vi module này).

Dùng đúng `increase()` để xử lý counter reset khi pod/Envoy restart (mục 2) — KHÔNG
tự viết logic bù trừ. Kiểm tra scrape gap bằng `up{job="istio-proxy"}` trước khi tin
kết quả (mục 2).

Thiết kế pluggable (Protocol), cùng tinh thần `selector_validation.py`/`config_collector.py`:
  - FakePrometheusClient : canned response, dùng cho test — không cần Prometheus thật.
  - HttpPrometheusClient : hiện thực thật, query HTTP API chuẩn của Prometheus
    (`GET /api/v1/query`), dùng lib `requests`. Không chạy được trong môi trường demo
    này (không có Prometheus thật), nhưng code đầy đủ để dùng khi lên cluster.

NOTE — điểm mở/diễn giải (KHÔNG thuộc trách nhiệm code Python ở đây, phải cấu hình sẵn trên
mesh trước khi chạy module này thật):

  1. Provenance test-harness (mục 3) — ĐÃ XÁC NHẬN LÀ GIỚI HẠN THẬT, KHÔNG PHẢI THIẾU CẤU
     HÌNH (chạy thật trên máy Bin, 2026-09-27, trial-01, 15 phút traffic qua waypoint): dưới
     Istio **ambient** (ztunnel + waypoint, xem `03_install_istio.sh`), nhãn Pod tuỳ ý
     (`test-harness`, `injection-id`) KHÔNG thể đọc được qua attribute expression
     `source.labels[...]` trong Telemetry API — mọi series trả về đúng literal "unknown",
     không phải rỗng/absent. Xác nhận qua chính comment trong source code Istio: "Source
     label matching is not supported in ambient mode, and thus not supported in waypoint
     routes" (istio/istio#61590, lý do thiết kế ở istio/istio#51565) — cơ chế "Metadata
     Exchange" (trao đổi toàn bộ node labels qua ALPN) mà `source.labels[...]` phụ thuộc chỉ
     tồn tại giữa 2 **sidecar**, ztunnel/waypoint không cài lại cơ chế này. Các attribute
     built-in (`source_workload`, `source_canonical_service`, `request.url_path`) KHÔNG bị
     ảnh hưởng — vẫn resolve đúng qua waypoint, vì chúng đến từ tra cứu SPIFFE identity ở
     istiod, không qua Metadata Exchange. Xem `manifests/telemetry-custom-labels.yaml` để
     biết chi tiết đầy đủ + vì sao đã BỎ 2 tagOverrides `test_harness`/`injection_id` khỏi
     cấu hình mesh (không có cách nào khiến chúng hoạt động dưới ambient).

     KHÔNG CHẶN pipeline chấm điểm thật: `evaluation.py`/`experiment.py` chỉ so khớp theo
     `identity_key().pair_key()` với `ground_truth` đã biết SẴN từ lúc sinh kịch bản
     (`scenario_pool.py`) — KHÔNG hề đọc `is_injected`/`injection_id`. Với kiến trúc hiện tại
     (mỗi trial 1 namespace riêng), `source_workload_namespace`/`destination_workload_namespace`
     (đã xác nhận resolve đúng) tự nó đã đủ phân biệt trial; `test_harness`/`injection_id` chỉ
     cần thiết cho phương án "hạ tầng dùng chung" (nhiều trial ảo chung 1 namespace, xem
     README "Việc còn thiếu") — CHƯA triển khai, nên giới hạn này hiện không có tác động thật.

     Module này CHỈ đọc nhãn `test_harness`/`injection_id` nếu Prometheus đã trả về (đồng
     nhất với ranh giới `KubernetesSelectorValidator`: "yêu cầu hạ tầng đã sẵn sàng, không tự
     dựng hạ tầng đó"). Nếu nhãn vắng mặt (LUÔN vắng mặt dưới ambient, xem trên),
     `is_injected=False` mặc định (an toàn hơn: không lỡ đánh dấu traffic thật là traffic
     test) — 2 field này vẫn giữ trong schema (không xoá) để không phá test cũ và để dùng lại
     ngay nếu sau này chuyển sang sidecar hoặc Istio thêm cơ chế tương đương cho ambient.

  2. Path cho phân loại HEALTHCHECK (TIER1_DESIGN.md mục 2): `istio_requests_total`
     KHÔNG có nhãn path theo mặc định (tránh nổ cardinality) — cần một
     `tagOverrides` tương tự trỏ `request.url_path` vào nhãn `request_path`. Thiếu
     nhãn này, `path=None` (pipeline suy luận vẫn chạy được, chỉ mất tín hiệu
     phân loại theo path — mục 2 TIER1_DESIGN.md vẫn còn nhánh phân loại theo nguồn
     `monitoring_source_services` độc lập với path).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from .schema import ServiceRef, TrafficSeries

# ---------------------------------------------------------------------------
# Metrics client — pluggable (Prometheus / VictoriaMetrics)
# ---------------------------------------------------------------------------


class PrometheusClient(Protocol):
    def query(self, promql: str, time: Optional[str] = None) -> list[dict]:
        """Instant query — trả về `data.result` thô của HTTP API Prometheus
        (list các {"metric": {...nhãn...}, "value": [ts, "giá trị dạng string"]})."""
        ...


class FakePrometheusClient:
    """Dùng cho test — không gọi Prometheus thật. `responses` map promql (khớp CHÍNH
    XÁC chuỗi query) -> list kết quả thô (đúng shape `data.result`).

    Cho phép đăng ký một `matcher` (hàm (promql, time) -> list kết quả | None) để test
    không cần liệt kê từng chuỗi PromQL đúng byte-for-byte."""

    def __init__(self, responses: dict[str, list[dict]] | None = None, matcher=None):
        self._responses = responses or {}
        self._matcher = matcher
        self.queries: list[str] = []

    def query(self, promql: str, time: Optional[str] = None) -> list[dict]:
        self.queries.append(promql)
        if promql in self._responses:
            return self._responses[promql]
        if self._matcher is not None:
            result = self._matcher(promql, time)
            if result is not None:
                return result
        return []


class PartialResultError(RuntimeError):
    """VictoriaMetrics trả `isPartial: true` — một phần vmstorage không trả lời."""


class HttpPrometheusClient:
    """Gọi `GET {base_url}/api/v1/query` (HTTP API Prometheus chuẩn).

    base_url ví dụ: Prometheus `http://localhost:9090`; VictoriaMetrics qua vmauth của
    LOG_METRIC `http://vmauth:8427/select/0/prometheus`."""

    def __init__(self, base_url: str, timeout_seconds: float = 30.0, headers: Optional[dict] = None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._headers = headers or {}

    def _get(self, promql: str, time: Optional[str]) -> dict:  # pragma: no cover - cần server thật
        import requests  # import lazy — chỉ cần khi thật sự query

        params = {"query": promql}
        if time is not None:
            params["time"] = time
        resp = requests.get(f"{self._base_url}/api/v1/query", params=params,
                            headers=self._headers, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _check(self, body: dict) -> list[dict]:
        if body.get("status") != "success":
            raise RuntimeError(f"Query metrics thất bại: {body}")
        return body["data"]["result"]

    def query(self, promql: str, time: Optional[str] = None) -> list[dict]:
        return self._check(self._get(promql, time))


class VictoriaMetricsClient(HttpPrometheusClient):
    """VictoriaMetrics cluster (vmselect, thường sau vmauth). Khác Prometheus ở 1 điểm quan
    trọng với Tầng 1: khi 1 vmstorage chết, vmselect vẫn trả kết quả nhưng gắn
    `isPartial: true`. Thiếu dữ liệu nguy hiểm hơn chậm dữ liệu (cạnh biến mất -> policy
    chặn nhầm — docs/01 của LOG_METRIC) nên mặc định NÉM LỖI thay vì dùng kết quả thiếu."""

    def __init__(self, base_url: str, timeout_seconds: float = 30.0, headers: Optional[dict] = None,
                 allow_partial: bool = False):
        super().__init__(base_url, timeout_seconds, headers)
        self.allow_partial = allow_partial
        self.partial_queries: list[str] = []

    def query(self, promql: str, time: Optional[str] = None) -> list[dict]:
        body = self._get(promql, time)
        if body.get("isPartial"):
            self.partial_queries.append(promql)
            if not self.allow_partial:
                raise PartialResultError(
                    f"VictoriaMetrics trả kết quả THIẾU (isPartial=true) cho: {promql} — "
                    "kiểm tra vmstorage, hoặc allow_partial=True nếu chấp nhận rủi ro thiếu cạnh."
                )
        return self._check(body)


def make_metrics_client(url: str, backend: Optional[str] = None, **kwargs) -> HttpPrometheusClient:
    """`backend`: "prometheus" | "victoriametrics" (mặc định đọc env TRAFFIC_METRICS_BACKEND,
    rồi tới "prometheus")."""
    backend = (backend or os.environ.get("TRAFFIC_METRICS_BACKEND") or "prometheus").strip().lower()
    if backend in ("victoriametrics", "vm", "victoria"):
        return VictoriaMetricsClient(url, **kwargs)
    if backend == "prometheus":
        kwargs.pop("allow_partial", None)
        return HttpPrometheusClient(url, **kwargs)
    raise ValueError(f"TRAFFIC_METRICS_BACKEND='{backend}' không hợp lệ — 'prometheus' | 'victoriametrics'")


# ---------------------------------------------------------------------------
# PromQL builders — mục 1, 2, 3, 6
# ---------------------------------------------------------------------------

DEFAULT_REPORTER = "waypoint"  # HTTP/gRPC (L7) do waypoint đo
DEFAULT_TCP_REPORTER = "waypoint"  # dữ liệu thật 08/10: TCP qua waypoint có nguồn THẬT; reporter=destination ghi nguồn là chính waypoint


def _label_filter(namespaces: Optional[list[str]], injected: Optional[bool], reporter: str = DEFAULT_REPORTER) -> str:
    clauses = [f'reporter="{reporter}"']
    if namespaces:
        ns_regex = "|".join(namespaces)
        clauses.append(f'destination_workload_namespace=~"{ns_regex}"')
    if injected is True:
        clauses.append('test_harness="true"')
    elif injected is False:
        clauses.append('test_harness!="true"')
    return "{" + ",".join(clauses) + "}"


def build_http_grpc_query(
    window_days: int, namespaces: Optional[list[str]] = None, injected: Optional[bool] = None
) -> str:
    """mục 1, 2: `increase(istio_requests_total{...}[Kd])` — bắt HTTP/gRPC."""
    return f"increase(istio_requests_total{_label_filter(namespaces, injected)}[{window_days}d])"


def build_tcp_query(
    window_days: int, namespaces: Optional[list[str]] = None, injected: Optional[bool] = None,
    reporter: str = DEFAULT_TCP_REPORTER,
) -> str:
    """mục 1: `istio_tcp_connections_opened_total` — Postgres/Redis/Kafka mà HTTP counter bỏ sót."""
    return f"increase(istio_tcp_connections_opened_total{_label_filter(namespaces, injected, reporter)}[{window_days}d])"


def _with(base: str, extra: str) -> str:
    return base[:-1] + "," + extra + "}"


def build_error_query(
    window_days: int, namespaces: Optional[list[str]] = None, injected: Optional[bool] = None
) -> str:
    """Phần lỗi HTTP: response_code 5xx (TIER1_DESIGN.md mục 9)."""
    with_error = _with(_label_filter(namespaces, injected), 'response_code=~"5.."')
    return f"increase(istio_requests_total{with_error}[{window_days}d])"


def build_grpc_error_query(
    window_days: int, namespaces: Optional[list[str]] = None, injected: Optional[bool] = None
) -> str:
    """Phần lỗi gRPC: grpc_response_status khác 0 nhưng response_code KHÔNG phải 5xx (gRPC
    lỗi vẫn trả HTTP 200; loại 5xx để không đếm 2 lần với build_error_query)."""
    with_error = _with(_label_filter(namespaces, injected),
                       'request_protocol="grpc",grpc_response_status!~"0|",response_code!~"5.."')
    return f"increase(istio_requests_total{with_error}[{window_days}d])"


def build_scrape_gap_query(job: str = "kubernetes-pods", window_days: Optional[int] = None) -> str:
    """mục 2: target data-plane (ztunnel, waypoint) có lúc nào `up == 0` trong cửa sổ không.
    `window_days=None` -> chỉ xét hiện tại (hành vi cũ)."""
    selectors = (f'job="{job}",app="ztunnel"', f'job="{job}",gateway_networking_k8s_io_gateway_name!=""')
    if window_days is None:
        return " or ".join(f"up{{{sel}}} == 0" for sel in selectors)
    return " or ".join(f"min_over_time(up{{{sel}}}[{window_days}d]) == 0" for sel in selectors)


def _day_end(now: datetime, day_offset: int) -> datetime:
    return (now - timedelta(days=day_offset)).replace(hour=23, minute=59, second=59, microsecond=0)


def build_daily_bucket_query(
    day_offset: int,
    now: datetime,
    namespaces: Optional[list[str]] = None,
    injected: Optional[bool] = None,
    metric: str = "istio_requests_total",
    reporter: str = DEFAULT_REPORTER,
) -> tuple[str, str]:
    """mục 6: 1 instant query `increase(...[1d] @ <cuối ngày>)` cho TỪNG ngày trong cửa sổ K
    ngày — tính `traffic_days`/`last_seen_days_ago`. Trả về (promql, ngày YYYY-MM-DD).
    Modifier `@` được cả Prometheus lẫn VictoriaMetrics hỗ trợ."""
    day_end = _day_end(now, day_offset)
    query = f"increase({metric}{_label_filter(namespaces, injected, reporter)}[1d] @ {day_end.timestamp():.0f})"
    return query, day_end.date().isoformat()


# ---------------------------------------------------------------------------
# Parsing — label set -> TrafficSeries
# ---------------------------------------------------------------------------

EXTERNAL_PSEUDO_SERVICES = ("PassthroughCluster", "BlackHoleCluster")


def _extract_service_account(principal: Optional[str]) -> Optional[str]:
    """SPIFFE URI chuẩn Istio: spiffe://<trust-domain>/ns/<namespace>/sa/<service-account>."""
    if not principal or "/sa/" not in principal:
        return None
    return principal.rsplit("/sa/", 1)[-1]


def _series_key(metric: dict) -> tuple:
    return (
        metric.get("source_workload_namespace", ""),
        metric.get("source_canonical_service", ""),
        metric.get("source_principal", ""),
        metric.get("destination_workload_namespace", ""),
        metric.get("destination_canonical_service", ""),
        metric.get("request_protocol", "tcp"),
        metric.get("destination_port"),
        metric.get("request_path"),
    )


def _parse_value(item: dict) -> float:
    try:
        return float(item["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return 0.0


@dataclass
class _Accumulator:
    metric: dict
    count: float = 0.0
    error_count: float = 0.0
    daily: dict[str, float] = field(default_factory=dict)  # ngày -> số request ngày đó
    last_seen_days_ago: Optional[float] = None


def parse_window_result(raw_results: list[dict], accumulators: dict[tuple, _Accumulator], field_name: str) -> None:
    for item in raw_results:
        metric = item.get("metric", {})
        key = _series_key(metric)
        acc = accumulators.get(key)
        if acc is None:
            acc = _Accumulator(metric=metric)
            accumulators[key] = acc
        setattr(acc, field_name, getattr(acc, field_name) + _parse_value(item))


def parse_daily_result(raw_results: list[dict], accumulators: dict[tuple, _Accumulator], day: str, day_offset: int) -> None:
    """Chỉ CHÚ THÍCH thêm (ngày có traffic, last_seen) vào accumulator đã tồn tại từ query
    tổng cửa sổ — query tổng mới là nguồn xác nhận series tồn tại; một giá trị 0 ở 1 ngày
    không tạo series "ma". Giữ SỐ REQUEST từng ngày (bản cũ chỉ giữ tập ngày rồi bỏ)."""
    for item in raw_results:
        value = _parse_value(item)
        if value <= 0:
            continue
        acc = accumulators.get(_series_key(item.get("metric", {})))
        if acc is None:
            continue
        acc.daily[day] = acc.daily.get(day, 0.0) + value
        if acc.last_seen_days_ago is None or day_offset < acc.last_seen_days_ago:
            acc.last_seen_days_ago = float(day_offset)


def _to_traffic_series(acc: _Accumulator, protocol_override: Optional[str] = None) -> TrafficSeries:
    m = acc.metric
    protocol = (protocol_override or m.get("request_protocol") or "TCP").upper()

    flags: list[str] = []
    dest_service_ref = None
    dest_service_name = m.get("destination_service_name")
    dest_service_ns = m.get("destination_service_namespace")
    if dest_service_name in EXTERNAL_PSEUDO_SERVICES:
        flags.append("external_unregistered")  # ra ngoài cluster, chưa khai ServiceEntry (mục 8)
    elif dest_service_name and dest_service_ns and dest_service_ns != "unknown":
        dest_service_ref = ServiceRef(dest_service_name, dest_service_ns)

    port_raw = m.get("destination_port")
    port = int(port_raw) if port_raw not in (None, "", "0") else None
    days = sorted(acc.daily)

    return TrafficSeries(
        source_namespace=m.get("source_workload_namespace", ""),
        source_canonical_service=m.get("source_canonical_service", ""),
        source_service_account=_extract_service_account(m.get("source_principal")) or "unknown",
        source_workload_namespace=m.get("source_workload_namespace", ""),
        destination_namespace=m.get("destination_workload_namespace", ""),
        destination_canonical_service=m.get("destination_canonical_service", ""),
        destination_service_account=_extract_service_account(m.get("destination_principal")),
        destination_service_ref=dest_service_ref,
        protocol=protocol,
        port=port,
        path=m.get("request_path"),
        count=int(acc.count),
        distinct_days=len(days),
        last_seen_days_ago=acc.last_seen_days_ago if acc.last_seen_days_ago is not None else 9999.0,
        error_count=int(acc.error_count),
        is_injected=(m.get("test_harness") == "true"),
        injection_id=m.get("injection_id"),
        workload_revision=m.get("destination_canonical_revision"),
        port_source="observed" if port is not None else "unknown",
        traffic_days=days,
        flags=flags,
        destination_service_host=m.get("destination_service"),
        evidence_source="metrics",
    )


# ---------------------------------------------------------------------------
# Scrape gap check — mục 2
# ---------------------------------------------------------------------------


@dataclass
class ScrapeGapReport:
    has_gap: bool
    affected_targets: list[str] = field(default_factory=list)


def check_scrape_gaps(client: PrometheusClient, job: str = "kubernetes-pods",
                      window_days: Optional[int] = None) -> ScrapeGapReport:
    """mục 2: target ztunnel/waypoint nào có gap scrape (trong cả cửa sổ nếu có
    `window_days`) — không tự viết logic bù trừ reset, chỉ cảnh báo."""
    raw = client.query(build_scrape_gap_query(job, window_days))
    targets = sorted({item.get("metric", {}).get("instance", "unknown") for item in raw})
    return ScrapeGapReport(has_gap=len(targets) > 0, affected_targets=targets)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@dataclass
class CollectionResult:
    series: list[TrafficSeries]
    scrape_gap: ScrapeGapReport
    # Số request theo ngày, CÙNG THỨ TỰ với `series` ({"2026-10-02": 3, ...}) — cho bảng
    # traffic_daily_counts của dashboard và ablation hysteresis (cắt snapshot theo ngày).
    daily_counts: list[dict[str, float]] = field(default_factory=list)
    log_rows_read: int = 0
    log_series_added: int = 0


def collect_traffic(
    client: PrometheusClient,
    window_days: int = 7,
    namespaces: Optional[list[str]] = None,
    injected: Optional[bool] = None,
    now: Optional[datetime] = None,
    check_gaps: bool = True,
    *,
    context=None,
    log_client=None,
    tcp_reporter: str = DEFAULT_TCP_REPORTER,
    scrape_job: str = "kubernetes-pods",
) -> CollectionResult:
    """Điều phối: scrape gap -> tổng cửa sổ (HTTP/gRPC + TCP) -> lỗi -> từng ngày (cả HTTP
    lẫn TCP) -> TrafficSeries -> [log ztunnel: port quan sát + cạnh TCP mới] ->
    [cluster context: suy port từ Service, cờ probe/monitoring/ServiceEntry].

    `context`: `cluster_context.ClusterContext` (None = không suy port, không gắn cờ).
    `log_client`: `log_collector.LogsClient` (None = chỉ dùng metrics).
    `injected`: None = không lọc; True = chỉ traffic test; False = chỉ traffic thật."""
    from .cluster_context import annotate_series

    now = now or datetime.now(timezone.utc)
    scrape_gap = (check_scrape_gaps(client, scrape_job, window_days) if check_gaps
                  else ScrapeGapReport(has_gap=False))

    accumulators: dict[tuple, _Accumulator] = {}
    tcp_accumulators: dict[tuple, _Accumulator] = {}

    parse_window_result(client.query(build_http_grpc_query(window_days, namespaces, injected)), accumulators, "count")
    parse_window_result(client.query(build_error_query(window_days, namespaces, injected)), accumulators, "error_count")
    parse_window_result(client.query(build_grpc_error_query(window_days, namespaces, injected)), accumulators, "error_count")
    parse_window_result(client.query(build_tcp_query(window_days, namespaces, injected, tcp_reporter)), tcp_accumulators, "count")

    for day_offset in range(window_days):
        query, day = build_daily_bucket_query(day_offset, now, namespaces, injected)
        parse_daily_result(client.query(query), accumulators, day, day_offset)
        tcp_query, _ = build_daily_bucket_query(day_offset, now, namespaces, injected,
                                                metric="istio_tcp_connections_opened_total", reporter=tcp_reporter)
        parse_daily_result(client.query(tcp_query), tcp_accumulators, day, day_offset)

    series: list[TrafficSeries] = []
    daily: list[dict[str, float]] = []
    for acc in accumulators.values():
        if acc.count > 0:
            series.append(_to_traffic_series(acc))
            daily.append(dict(sorted(acc.daily.items())))
    for acc in tcp_accumulators.values():
        if acc.count > 0:
            series.append(_to_traffic_series(acc, protocol_override="TCP"))
            daily.append(dict(sorted(acc.daily.items())))

    log_rows = 0
    log_added = 0
    if log_client is not None:
        from .log_collector import collect_ztunnel_flows, merge_log_series

        logs = collect_ztunnel_flows(
            log_client, window_days=window_days, namespaces=namespaces, now=now,
            workload_canonical=getattr(context, "workload_canonical", None),
        )
        log_rows = logs.rows_read
        kept, kept_daily = merge_log_series(series, logs.series, logs.daily_counts)
        series.extend(kept)
        daily.extend(kept_daily)
        log_added = len(kept)

    annotate_series(series, context)
    return CollectionResult(series=series, scrape_gap=scrape_gap, daily_counts=daily,
                            log_rows_read=log_rows, log_series_added=log_added)
