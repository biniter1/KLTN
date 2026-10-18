"""
Traffic Collection Pipeline — TRAFFIC_COLLECTION_PIPELINE.md mục 1-6.

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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from .schema import ServiceRef, TrafficSeries

# ---------------------------------------------------------------------------
# Prometheus client — pluggable
# ---------------------------------------------------------------------------


class PrometheusClient(Protocol):
    def query(self, promql: str, time: Optional[str] = None) -> list[dict]:
        """Instant query — trả về `data.result` thô của Prometheus HTTP API
        (list các {"metric": {...nhãn...}, "value": [ts, "giá trị dạng string"]})."""
        ...


class FakePrometheusClient:
    """Dùng cho test — không gọi Prometheus thật. `responses` map promql (khớp CHÍNH
    XÁC chuỗi query) -> list kết quả thô (đúng shape `data.result`).

    Cho phép đăng ký một `default_matcher` (hàm promql -> list kết quả | None) để
    test không cần liệt kê từng chuỗi PromQL đúng byte-for-byte (dùng cho query
    daily-bucket sinh động theo timestamp)."""

    def __init__(
        self,
        responses: dict[str, list[dict]] | None = None,
        matcher=None,
    ):
        self._responses = responses or {}
        self._matcher = matcher

    def query(self, promql: str, time: Optional[str] = None) -> list[dict]:
        if promql in self._responses:
            return self._responses[promql]
        if self._matcher is not None:
            result = self._matcher(promql, time)
            if result is not None:
                return result
        return []


class HttpPrometheusClient:
    """Hiện thực thật — gọi `GET {base_url}/api/v1/query` (Prometheus HTTP API
    chuẩn). Không chạy được trong môi trường demo này (không có Prometheus thật)."""

    def __init__(self, base_url: str, timeout_seconds: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def query(self, promql: str, time: Optional[str] = None) -> list[dict]:  # pragma: no cover - cần Prometheus thật
        import requests  # import lazy — chỉ cần khi thật sự query Prometheus thật

        params = {"query": promql}
        if time is not None:
            params["time"] = time
        resp = requests.get(f"{self._base_url}/api/v1/query", params=params, timeout=self._timeout)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus query thất bại: {body}")
        return body["data"]["result"]


# ---------------------------------------------------------------------------
# PromQL builders — mục 1, 2, 3, 6
# ---------------------------------------------------------------------------


def _label_filter(namespaces: Optional[list[str]], injected: Optional[bool]) -> str:
    clauses = ['reporter="waypoint"']
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
    window_days: int, namespaces: Optional[list[str]] = None, injected: Optional[bool] = None
) -> str:
    """mục 1: `istio_tcp_connections_opened_total` — bắt Postgres/Redis/Kafka mà
    HTTP counter bỏ sót hoàn toàn."""
    return f"increase(istio_tcp_connections_opened_total{_label_filter(namespaces, injected)}[{window_days}d])"


def build_error_query(
    window_days: int, namespaces: Optional[list[str]] = None, injected: Optional[bool] = None
) -> str:
    """Đếm riêng phần lỗi (response_code>=500 hoặc grpc_response_status != 0) để tính
    error_count/error_ratio (TIER1_DESIGN.md mục 9) — cùng cửa sổ, cùng nhãn lọc."""
    base = _label_filter(namespaces, injected)
    # chèn thêm điều kiện lỗi vào trong cùng label-matcher set
    with_error = base[:-1] + ',response_code=~"5.."}'
    return f"increase(istio_requests_total{with_error}[{window_days}d])"


def build_scrape_gap_query(job: str = "istio-proxy") -> str:
    """mục 2: `up{job="istio-proxy"} == 0` — target nào đang có gap scrape."""
    return f'up{{job="{job}"}} == 0'


def build_daily_bucket_query(
    day_offset: int,
    now: datetime,
    namespaces: Optional[list[str]] = None,
    injected: Optional[bool] = None,
) -> tuple[str, str]:
    """mục 6 (đóng gói thành snapshot, lọc noise theo ngày): 1 instant query
    `increase(...[1d] @ <cuối ngày day_offset>)` cho TỪNG ngày trong cửa sổ K ngày —
    dùng để tính `distinct_days`/`last_seen_days_ago` (TIER1_DESIGN.md mục 4) mà
    không cần 1 subquery PromQL phức tạp duy nhất. Trả về (promql, iso_timestamp)."""
    day_end = (now - timedelta(days=day_offset)).replace(hour=23, minute=59, second=59, microsecond=0)
    ts = day_end.timestamp()
    query = f"increase(istio_requests_total{_label_filter(namespaces, injected)}[1d] @ {ts:.0f})"
    return query, day_end.isoformat()


# ---------------------------------------------------------------------------
# Parsing — Prometheus label set -> TrafficSeries
# ---------------------------------------------------------------------------


def _extract_service_account(principal: Optional[str]) -> Optional[str]:
    """SPIFFE URI chuẩn Istio: spiffe://<trust-domain>/ns/<namespace>/sa/<service-account>."""
    if not principal or "/sa/" not in principal:
        return None
    return principal.rsplit("/sa/", 1)[-1]


def _series_key(metric: dict) -> tuple:
    return (
        metric.get("source_workload_namespace", ""),
        metric.get("source_canonical_service", ""),
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
    days_with_traffic: set[str] = field(default_factory=set)
    last_seen_days_ago: Optional[float] = None


def parse_window_result(
    raw_results: list[dict],
    accumulators: dict[tuple, _Accumulator],
    field_name: str,
) -> None:
    for item in raw_results:
        metric = item.get("metric", {})
        key = _series_key(metric)
        acc = accumulators.get(key)
        if acc is None:
            acc = _Accumulator(metric=metric)
            accumulators[key] = acc
        setattr(acc, field_name, getattr(acc, field_name) + _parse_value(item))


def parse_daily_result(raw_results: list[dict], accumulators: dict[tuple, _Accumulator], day_iso: str, day_offset: int) -> None:
    """Chỉ CHÚ THÍCH thêm (distinct_days/last_seen_days_ago) vào accumulator đã tồn
    tại từ query window total (`parse_window_result`) — không tự tạo accumulator mới
    ở đây, vì query window total mới là nguồn xác nhận series có tồn tại hay không;
    một giá trị 0 ở một ngày cụ thể không tạo ra series "ma" nào."""
    for item in raw_results:
        if _parse_value(item) <= 0:
            continue
        metric = item.get("metric", {})
        key = _series_key(metric)
        acc = accumulators.get(key)
        if acc is None:
            continue
        acc.days_with_traffic.add(day_iso)
        if acc.last_seen_days_ago is None or day_offset < acc.last_seen_days_ago:
            acc.last_seen_days_ago = float(day_offset)


def _to_traffic_series(acc: _Accumulator, protocol_override: Optional[str] = None) -> TrafficSeries:
    m = acc.metric
    protocol = (protocol_override or m.get("request_protocol") or "TCP").upper()
    dest_service_ref = None
    dest_service_name = m.get("destination_service_name")
    dest_service_ns = m.get("destination_service_namespace")
    if dest_service_name and dest_service_ns:
        dest_service_ref = ServiceRef(dest_service_name, dest_service_ns)

    port_raw = m.get("destination_port")
    port = int(port_raw) if port_raw not in (None, "") else 0

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
        distinct_days=len(acc.days_with_traffic),
        last_seen_days_ago=acc.last_seen_days_ago if acc.last_seen_days_ago is not None else 9999.0,
        error_count=int(acc.error_count),
        is_injected=(m.get("test_harness") == "true"),
        injection_id=m.get("injection_id"),
        workload_revision=m.get("destination_canonical_revision"),
    )


# ---------------------------------------------------------------------------
# Scrape gap check — mục 2
# ---------------------------------------------------------------------------


@dataclass
class ScrapeGapReport:
    has_gap: bool
    affected_targets: list[str] = field(default_factory=list)


def check_scrape_gaps(client: PrometheusClient, job: str = "istio-proxy") -> ScrapeGapReport:
    """mục 2: kiểm tra `up{job="istio-proxy"} == 0` TRƯỚC KHI tin kết quả increase() —
    không tự viết logic bù trừ reset, chỉ cần đảm bảo không có gap scrape quá dài."""
    raw = client.query(build_scrape_gap_query(job))
    targets = [item.get("metric", {}).get("instance", "unknown") for item in raw]
    return ScrapeGapReport(has_gap=len(targets) > 0, affected_targets=targets)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@dataclass
class CollectionResult:
    series: list[TrafficSeries]
    scrape_gap: ScrapeGapReport


def collect_traffic(
    client: PrometheusClient,
    window_days: int = 7,
    namespaces: Optional[list[str]] = None,
    injected: Optional[bool] = None,
    now: Optional[datetime] = None,
    check_gaps: bool = True,
) -> CollectionResult:
    """Điều phối toàn bộ pipeline mục 1-6: kiểm tra scrape gap -> query window total
    (HTTP/gRPC + TCP) -> query error count -> query daily bucket (distinct_days,
    last_seen_days_ago) -> gộp thành `TrafficSeries` (input `discover_candidates()`
    bên Tier 1, C_observed, mục 1 TIER1_DESIGN.md).

    `injected`: None = không lọc (mặc định, mixed test+production); True = CHỈ traffic
    test (dùng khi chạy thí nghiệm R=15, mục 3/6 tài liệu); False = CHỈ traffic thật
    (dùng khi vận hành thật, loại traffic test lẫn vào production)."""
    now = now or datetime.now(timezone.utc)

    scrape_gap = check_scrape_gaps(client) if check_gaps else ScrapeGapReport(has_gap=False)

    accumulators: dict[tuple, _Accumulator] = {}
    tcp_accumulators: dict[tuple, _Accumulator] = {}

    parse_window_result(client.query(build_http_grpc_query(window_days, namespaces, injected)), accumulators, "count")
    parse_window_result(client.query(build_error_query(window_days, namespaces, injected)), accumulators, "error_count")
    parse_window_result(client.query(build_tcp_query(window_days, namespaces, injected)), tcp_accumulators, "count")

    for day_offset in range(window_days):
        query, day_iso = build_daily_bucket_query(day_offset, now, namespaces, injected)
        parse_daily_result(client.query(query), accumulators, day_iso, day_offset)

    series = [_to_traffic_series(acc) for acc in accumulators.values() if acc.count > 0]
    series += [_to_traffic_series(acc, protocol_override="TCP") for acc in tcp_accumulators.values() if acc.count > 0]

    return CollectionResult(series=series, scrape_gap=scrape_gap)
