"""
Dữ liệu giả lập phong cách Online Boutique (đúng app nền đã chốt ở
CONFIG_EVIDENCE_COLLECTOR.md mục 2) — KHÔNG cần cluster/Prometheus thật.

Phủ đủ các nhánh quan trọng của Tier 1 để demo end-to-end:

  edge                                     -> kết quả kỳ vọng
  ------------------------------------------------------------------
  frontend -> productcatalogservice        -> OBSERVED_STRONG (traffic mạnh + config khớp)
  frontend -> currencyservice              -> OBSERVED_WEAK   (traffic ít, thưa ngày)
  frontend -> paymentservice               -> UNKNOWN         (protocol lạ, class không phân loại được)
  blackbox-exporter -> frontend            -> OBSERVED_WEAK   (toàn traffic MONITORING, bị hạ điểm)
  checkoutservice -> emailservice          -> NOT_OBSERVED + needs_human_review (config sạch, 0 traffic)
  checkoutservice -> shipping-backend      -> NOT_OBSERVED + escalate Tier 2   (config FOUND_UNRESOLVED, 0 traffic)
  cartservice -> redis-cart                -> NEEDS_MANUAL_SELECTOR (traffic mạnh nhưng selector "hỏng" - mock)
  frontend -> kube-dns                     -> DNS auto-allow (mục 7, tách khỏi 5-state)
  checkoutservice -> api.stripe.com        -> OBSERVED_STRONG, scope=EXTERNAL (mục 8)

Hàm build_snapshot(variant) trả về 2 biến thể ("run1", "run2") để examples/run_example.py
demo hysteresis (mục 5b): ở run2, traffic của productcatalogservice giảm xuống vùng
"chết" giữa theta_demote/theta_promote — kỳ vọng NÓ VẪN GIỮ NGUYÊN OBSERVED_STRONG
nhờ previous_status, thay vì rớt ngay xuống WEAK.
"""

from __future__ import annotations

from tier1.schema import ConfigEvidenceRecord, ConfigFinding, Scope, ServiceRef, TrafficSeries

NS = "default"


def _traffic_run1() -> list[TrafficSeries]:
    return [
        # --- frontend -> productcatalogservice : traffic mạnh, đa số BUSINESS, ít noise healthcheck ---
        TrafficSeries(
            source_namespace=NS, source_canonical_service="frontend", source_service_account="frontend-sa",
            source_workload_namespace=NS,
            destination_namespace=NS, destination_canonical_service="productcatalogservice",
            destination_service_account="productcatalog-sa",
            destination_service_ref=ServiceRef("productcatalogservice", NS),
            protocol="GRPC", port=3550, path="/hipstershop.ProductCatalogService/ListProducts",
            count=1200, distinct_days=7, last_seen_days_ago=0.1, error_count=5,
        ),
        TrafficSeries(
            source_namespace=NS, source_canonical_service="frontend", source_service_account="frontend-sa",
            source_workload_namespace=NS,
            destination_namespace=NS, destination_canonical_service="productcatalogservice",
            destination_service_account="productcatalog-sa",
            destination_service_ref=ServiceRef("productcatalogservice", NS),
            protocol="GRPC", port=3550, path="/grpc.health.v1.Health/Check",
            count=50, distinct_days=7, last_seen_days_ago=0.1, error_count=0,
        ),

        # --- frontend -> currencyservice : traffic ít, thưa ngày -> kỳ vọng WEAK ---
        TrafficSeries(
            source_namespace=NS, source_canonical_service="frontend", source_service_account="frontend-sa",
            source_workload_namespace=NS,
            destination_namespace=NS, destination_canonical_service="currencyservice",
            destination_service_account="currency-sa",
            destination_service_ref=ServiceRef("currencyservice", NS),
            protocol="GRPC", port=7000, path="/hipstershop.CurrencyService/Convert",
            count=8, distinct_days=2, last_seen_days_ago=1.0, error_count=0,
        ),

        # --- frontend -> paymentservice : protocol lạ, không rule nào khớp -> class UNKNOWN toàn bộ ---
        TrafficSeries(
            source_namespace=NS, source_canonical_service="frontend", source_service_account="frontend-sa",
            source_workload_namespace=NS,
            destination_namespace=NS, destination_canonical_service="paymentservice",
            destination_service_account="payment-sa",
            destination_service_ref=ServiceRef("paymentservice", NS),
            protocol="CUSTOM-RPC", port=50051, path=None,
            count=40, distinct_days=3, last_seen_days_ago=2.0, error_count=0,
        ),

        # --- blackbox-exporter -> frontend : toàn traffic MONITORING (path "/" không khớp healthcheck) ---
        TrafficSeries(
            source_namespace="monitoring", source_canonical_service="blackbox-exporter",
            source_service_account="blackbox-sa", source_workload_namespace="monitoring",
            destination_namespace=NS, destination_canonical_service="frontend",
            destination_service_account="frontend-sa",
            destination_service_ref=ServiceRef("frontend", NS),
            protocol="HTTP", port=80, path="/",
            count=100, distinct_days=7, last_seen_days_ago=0.05, error_count=0,
        ),

        # --- cartservice -> redis-cart : traffic TCP mạnh -> sẽ đạt STRONG rồi bị hạ vì selector "hỏng" (mock) ---
        TrafficSeries(
            source_namespace=NS, source_canonical_service="cartservice", source_service_account="cart-sa",
            source_workload_namespace=NS,
            destination_namespace=NS, destination_canonical_service="redis-cart",
            destination_service_account=None,
            destination_service_ref=ServiceRef("redis-cart", NS),
            protocol="TCP", port=6379, path=None,
            count=500, distinct_days=7, last_seen_days_ago=0.05, error_count=2,
        ),

        # --- frontend -> kube-dns : DNS, auto-allow, tách khỏi 5-state ---
        TrafficSeries(
            source_namespace=NS, source_canonical_service="frontend", source_service_account="frontend-sa",
            source_workload_namespace=NS,
            destination_namespace="kube-system", destination_canonical_service="kube-dns",
            destination_service_account="kube-dns-sa",
            destination_service_ref=ServiceRef("kube-dns", "kube-system"),
            protocol="DNS", port=53, path=None,
            count=300, distinct_days=7, last_seen_days_ago=0.01, error_count=0,
        ),

        # --- checkoutservice -> api.stripe.com : EXTERNAL scope (mục 8), traffic mạnh -> STRONG ---
        TrafficSeries(
            source_namespace=NS, source_canonical_service="checkoutservice", source_service_account="checkout-sa",
            source_workload_namespace=NS,
            destination_namespace=NS, destination_canonical_service="api.stripe.com",
            destination_service_account=None,
            destination_service_ref=None,  # không resolve được K8s Service nội bộ -> EXTERNAL (dns_external.py)
            protocol="HTTP", port=443, path="/v1/charges",
            count=200, distinct_days=6, last_seen_days_ago=0.2, error_count=1,
        ),
    ]


def _traffic_run2() -> list[TrafficSeries]:
    """Biến thể thứ 2, cùng bộ edge, đổi số liệu productcatalogservice và currencyservice
    để demo hysteresis mục 5b — các edge khác giữ hệt run1 cho gọn."""
    series = _traffic_run1()
    for s in series:
        if s.destination_canonical_service == "productcatalogservice" and s.path and "ListProducts" in s.path:
            # Traffic giảm mạnh (giả lập một ngày traffic thấp bất thường) — điểm số sẽ tụt
            # xuống vùng "chết" [theta_demote, theta_promote) nếu tính từ đầu, nhưng vì
            # previous_status=STRONG nên phải tụt dưới theta_demote MỚI bị hạ (mục 5b).
            s.count = 300
            s.distinct_days = 4
            s.last_seen_days_ago = 0.5
        if s.destination_canonical_service == "currencyservice":
            # Traffic tăng nhẹ nhưng vẫn chưa đủ theta_promote -> phải TIẾP TỤC là WEAK
            # (vì trước đó không phải STRONG, hysteresis đòi hỏi >= theta_promote mới lên STRONG).
            s.count = 15
            s.distinct_days = 3
    return series


def _config_evidence(snapshot_id: str) -> list[ConfigEvidenceRecord]:
    collected_at = "2026-09-19T00:00:00Z"
    return [
        # productcatalogservice: config khớp sạch, củng cố thêm cho traffic đã mạnh sẵn (mục 4a)
        ConfigEvidenceRecord(
            source_namespace=NS, source_canonical_service="frontend", source_service_account="frontend-sa",
            destination_namespace=NS, destination_canonical_service="productcatalogservice",
            destination_service_account="productcatalog-sa",
            protocol="GRPC", port=3550, port_unknown=False,
            config_finding=ConfigFinding.FOUND_EMBEDDED, evidence_source="env", scope=Scope.IN_CLUSTER,
            snapshot_id=snapshot_id, collected_at=collected_at,
        ),
        # redis-cart: env REDIS_ADDR=redis-cart:6379
        ConfigEvidenceRecord(
            source_namespace=NS, source_canonical_service="cartservice", source_service_account="cart-sa",
            destination_namespace=NS, destination_canonical_service="redis-cart",
            destination_service_account=None,
            protocol="TCP", port=6379, port_unknown=False,
            config_finding=ConfigFinding.FOUND_EMBEDDED, evidence_source="env", scope=Scope.IN_CLUSTER,
            snapshot_id=snapshot_id, collected_at=collected_at,
        ),
        # checkoutservice -> emailservice: KHÔNG có traffic, nhưng config sạch -> NOT_OBSERVED + review người
        ConfigEvidenceRecord(
            source_namespace=NS, source_canonical_service="checkoutservice", source_service_account="checkout-sa",
            destination_namespace=NS, destination_canonical_service="emailservice",
            destination_service_account="email-sa",
            protocol="GRPC", port=5000, port_unknown=False,
            config_finding=ConfigFinding.FOUND_EMBEDDED, evidence_source="env", scope=Scope.IN_CLUSTER,
            snapshot_id=snapshot_id, collected_at=collected_at,
        ),
        # checkoutservice -> "shipping-backend": tên KHÔNG khớp Service thật nào (đúng ra là
        # "shippingservice") -> FOUND_UNRESOLVED, không có traffic -> NOT_OBSERVED + escalate Tier2
        ConfigEvidenceRecord(
            source_namespace=NS, source_canonical_service="checkoutservice", source_service_account="checkout-sa",
            destination_namespace=NS, destination_canonical_service="shipping-backend",
            destination_service_account=None,
            protocol=None, port=None, port_unknown=True,
            config_finding=ConfigFinding.FOUND_UNRESOLVED, evidence_source="env", scope=Scope.UNKNOWN,
            snapshot_id=snapshot_id, collected_at=collected_at,
        ),
        # checkoutservice -> api.stripe.com: củng cố thêm cho edge EXTERNAL
        ConfigEvidenceRecord(
            source_namespace=NS, source_canonical_service="checkoutservice", source_service_account="checkout-sa",
            destination_namespace=NS, destination_canonical_service="api.stripe.com",
            destination_service_account=None,
            protocol="HTTP", port=443, port_unknown=False,
            config_finding=ConfigFinding.FOUND_EMBEDDED, evidence_source="env", scope=Scope.EXTERNAL,
            snapshot_id=snapshot_id, collected_at=collected_at,
        ),
    ]


def build_snapshot(variant: str) -> tuple[list[TrafficSeries], list[ConfigEvidenceRecord], str]:
    """variant in {"run1", "run2"}"""
    snapshot_id = f"snap_{variant}"
    traffic = _traffic_run1() if variant == "run1" else _traffic_run2()
    config_ev = _config_evidence(snapshot_id)
    return traffic, config_ev, snapshot_id
