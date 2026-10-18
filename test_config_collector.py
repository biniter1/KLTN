"""config_collector.py — CONFIG_EVIDENCE_COLLECTOR.md mục 1-7 (FakeK8sReader, không cần cluster)."""

from __future__ import annotations

from tier1.config_collector import (
    ContainerSpec,
    EndpointsInfo,
    FakeK8sReader,
    PodInfo,
    ServiceInfo,
    _dedupe_candidates,
    _looks_like_address,
    _remove_self_references,
    _resolve_against_cluster,
    collect_config_evidence,
    pair_host_port_vars,
    parse_candidate_addresses,
)
from tier1.schema import ConfigFinding, Scope

NS = "default"


def _pod(name, labels=None, sa="default", containers=None, namespace=NS) -> PodInfo:
    return PodInfo(
        name=name,
        namespace=namespace,
        service_account_name=sa,
        labels=labels or {},
        containers=containers or [],
    )


# ---------------------------------------------------------------------------
# Bước 1-2 — parse + lọc cú pháp
# ---------------------------------------------------------------------------


def test_looks_like_address_rejects_pure_number():
    assert _looks_like_address("30") is False


def test_looks_like_address_rejects_whitespace():
    assert _looks_like_address("hello world") is False


def test_looks_like_address_rejects_empty():
    assert _looks_like_address("") is False


def test_looks_like_address_accepts_bare_name():
    assert _looks_like_address("productcatalogservice") is True


def test_looks_like_address_accepts_fqdn():
    assert _looks_like_address("productcatalogservice.default.svc.cluster.local") is True


def test_parse_level1_scheme():
    result = parse_candidate_addresses({"URL": "http://productcatalogservice:3550/path"}, "env")
    assert len(result) == 1
    assert result[0].host == "productcatalogservice"
    assert result[0].port == 3550
    assert result[0].port_unknown is False


def test_parse_level2_host_port():
    result = parse_candidate_addresses({"ADDR": "productcatalogservice:3550"}, "env")
    assert len(result) == 1
    assert result[0].host == "productcatalogservice"
    assert result[0].port == 3550


def test_parse_level3_bare_name():
    result = parse_candidate_addresses({"TARGET": "productcatalogservice"}, "env")
    assert len(result) == 1
    assert result[0].host == "productcatalogservice"
    assert result[0].port is None
    assert result[0].port_unknown is True


def test_parse_filters_out_timeout_number():
    result = parse_candidate_addresses({"TIMEOUT": "30"}, "env")
    assert result == []


def test_parse_skips_empty_value():
    result = parse_candidate_addresses({"EMPTY": ""}, "env")
    assert result == []


# ---------------------------------------------------------------------------
# Bước 3 — ghép cặp HOST/PORT
# ---------------------------------------------------------------------------


def test_pair_host_port_combines_matching_prefix():
    env = {"DB_HOST": "postgres", "DB_PORT": "5432"}
    result = pair_host_port_vars(env)
    assert result == {"DB_HOST": "postgres:5432"}


def test_pair_addr_port_combines():
    env = {"CACHE_ADDR": "redis-cart", "CACHE_PORT": "6379"}
    result = pair_host_port_vars(env)
    assert result == {"CACHE_ADDR": "redis-cart:6379"}


def test_pair_does_not_merge_unrelated_prefix():
    env = {"DB_HOST": "postgres", "DB_MAX_CONNECTIONS": "10"}
    result = pair_host_port_vars(env)
    # DB_HOST không tìm thấy DB_PORT chính xác -> giữ nguyên giá trị gốc
    assert result["DB_HOST"] == "postgres"
    assert result["DB_MAX_CONNECTIONS"] == "10"


def test_pair_leaves_unrelated_vars_untouched():
    env = {"LOG_LEVEL": "info", "DB_HOST": "postgres", "DB_PORT": "5432"}
    result = pair_host_port_vars(env)
    assert result["LOG_LEVEL"] == "info"
    assert result["DB_HOST"] == "postgres:5432"
    assert "DB_PORT" not in result  # đã bị "tiêu thụ" vào cặp


# ---------------------------------------------------------------------------
# Bước 4 — khử trùng
# ---------------------------------------------------------------------------


def test_dedupe_prefers_env_over_envfrom():
    candidates = parse_candidate_addresses({"TARGET": "productcatalogservice"}, "env")
    candidates += parse_candidate_addresses({"TARGET2": "productcatalogservice"}, "envFrom")
    # cùng host+port(None)+raw_value -> coi là trùng
    deduped = _dedupe_candidates(candidates)
    assert len(deduped) == 1
    assert deduped[0].evidence_source == "env"


def test_dedupe_keeps_distinct_hosts():
    candidates = parse_candidate_addresses({"A": "productcatalogservice", "B": "currencyservice"}, "env")
    deduped = _dedupe_candidates(candidates)
    assert len(deduped) == 2


# ---------------------------------------------------------------------------
# Bước 5 — loại tự tham chiếu
# ---------------------------------------------------------------------------


def test_remove_self_reference():
    candidates = parse_candidate_addresses(
        {"SERVICE_NAME": "frontend", "TARGET": "productcatalogservice"}, "env"
    )
    filtered = _remove_self_references(candidates, own_names={"frontend"})
    assert len(filtered) == 1
    assert filtered[0].host == "productcatalogservice"


# ---------------------------------------------------------------------------
# Bước 6 — xác nhận ngược với K8s
# ---------------------------------------------------------------------------


def test_resolve_matches_real_service_with_port():
    candidates = parse_candidate_addresses({"ADDR": "productcatalogservice:3550"}, "env")
    services = {"productcatalogservice": ServiceInfo(name="productcatalogservice", namespace=NS, selector={"app": "productcatalog"})}
    resolved = _resolve_against_cluster(candidates, services, {})
    assert resolved[0].finding == ConfigFinding.FOUND_STRUCTURAL
    assert resolved[0].scope == Scope.IN_CLUSTER


def test_resolve_matches_real_service_without_port_is_embedded():
    candidates = parse_candidate_addresses({"TARGET": "productcatalogservice"}, "env")
    services = {"productcatalogservice": ServiceInfo(name="productcatalogservice", namespace=NS, selector={"app": "productcatalog"})}
    resolved = _resolve_against_cluster(candidates, services, {})
    assert resolved[0].finding == ConfigFinding.FOUND_EMBEDDED


def test_resolve_headless_endpoints_is_external():
    candidates = parse_candidate_addresses({"TARGET": "legacy-db"}, "env")
    endpoints = {"legacy-db": EndpointsInfo(service_name="legacy-db", namespace=NS, addresses=["10.20.30.40"])}
    resolved = _resolve_against_cluster(candidates, {}, endpoints)
    assert resolved[0].scope == Scope.EXTERNAL
    assert resolved[0].matched_endpoints is not None


def test_resolve_external_domain_unresolved():
    candidates = parse_candidate_addresses({"TARGET": "api.stripe.com"}, "env")
    resolved = _resolve_against_cluster(candidates, {}, {})
    assert resolved[0].scope == Scope.EXTERNAL
    assert resolved[0].finding == ConfigFinding.FOUND_UNRESOLVED


def test_resolve_unknown_bare_name_kept_not_deleted():
    """Luật cứng mục 1: 'không tìm thấy' KHÔNG BAO GIỜ là bằng chứng phủ định -> giữ lại, gắn cờ."""
    candidates = parse_candidate_addresses({"CACHE_BACKEND": "somecache"}, "env")
    resolved = _resolve_against_cluster(candidates, {}, {})
    assert len(resolved) == 1
    assert resolved[0].finding == ConfigFinding.FOUND_UNRESOLVED
    assert resolved[0].scope == Scope.UNKNOWN


# ---------------------------------------------------------------------------
# Integration — collect_config_evidence end-to-end
# ---------------------------------------------------------------------------


def _online_boutique_reader() -> FakeK8sReader:
    frontend_pod = _pod(
        "frontend-abc",
        labels={"security.istio.io/canonical-name": "frontend", "app": "frontend"},
        sa="frontend-sa",
        containers=[
            ContainerSpec(
                name="server",
                env={
                    "PRODUCT_CATALOG_ADDR": "productcatalogservice",
                    "PRODUCT_CATALOG_PORT": "3550",
                    "PAYMENT_ENDPOINT": "http://unknownpayments:9999",
                    "LOG_LEVEL": "info",
                    "TIMEOUT": "30",
                    "SERVICE_NAME": "frontend",  # tự tham chiếu, phải bị loại
                },
            )
        ],
    )
    productcatalog_pod = _pod(
        "productcatalog-xyz",
        labels={"security.istio.io/canonical-name": "productcatalogservice", "app": "productcatalog"},
        sa="productcatalog-sa",
    )
    unlabeled_pod = _pod("no-sidecar-pod", labels={"app": "legacy"})  # chưa được Istio admission gắn nhãn

    services = [
        ServiceInfo(name="productcatalogservice", namespace=NS, selector={"app": "productcatalog"}),
        ServiceInfo(name="frontend", namespace=NS, selector={"app": "frontend"}),
    ]

    return FakeK8sReader(
        pods=[frontend_pod, productcatalog_pod, unlabeled_pod],
        services=services,
    )


def test_collect_config_evidence_resolves_clean_match():
    reader = _online_boutique_reader()
    records = collect_config_evidence(reader, snapshot_id="snap_test")

    clean = [r for r in records if r.destination_canonical_service == "productcatalogservice"]
    assert len(clean) == 1
    assert clean[0].source_canonical_service == "frontend"
    assert clean[0].source_service_account == "frontend-sa"
    assert clean[0].port == 3550
    assert clean[0].config_finding == ConfigFinding.FOUND_STRUCTURAL
    assert clean[0].scope == Scope.IN_CLUSTER


def test_collect_config_evidence_keeps_unresolved_not_as_negative_evidence():
    reader = _online_boutique_reader()
    records = collect_config_evidence(reader, snapshot_id="snap_test")

    unresolved = [r for r in records if r.destination_canonical_service == "unknownpayments"]
    assert len(unresolved) == 1
    assert unresolved[0].config_finding == ConfigFinding.FOUND_UNRESOLVED


def test_collect_config_evidence_drops_self_reference():
    reader = _online_boutique_reader()
    records = collect_config_evidence(reader, snapshot_id="snap_test")
    assert all(r.destination_canonical_service != "frontend" for r in records)


def test_collect_config_evidence_timeout_number_never_becomes_evidence():
    """"TIMEOUT=30" bị loại thật sự ở bước 2 (thuần số, không phải cú pháp địa chỉ)."""
    reader = _online_boutique_reader()
    records = collect_config_evidence(reader, snapshot_id="snap_test")
    dest_names = {r.destination_canonical_service for r in records}
    assert "30" not in dest_names


def test_collect_config_evidence_ambiguous_bare_word_kept_as_unresolved():
    """"LOG_LEVEL=info" khớp cú pháp cấp 3 (bước 2 không phân biệt được với 1 tên
    Service hợp lệ) -> bị loại THẬT SỰ ở bước 6 vì không khớp Service nào, nhưng vẫn
    GIỮ LẠI làm evidence FOUND_UNRESOLVED (luật cứng mục 1: không tìm thấy != bằng
    chứng phủ định) — xem NOTE trong docstring module."""
    reader = _online_boutique_reader()
    records = collect_config_evidence(reader, snapshot_id="snap_test")
    info_records = [r for r in records if r.destination_canonical_service == "info"]
    assert len(info_records) == 1
    assert info_records[0].config_finding == ConfigFinding.FOUND_UNRESOLVED
    assert info_records[0].scope == Scope.UNKNOWN


def test_collect_config_evidence_skips_pod_without_istio_label():
    """Pod chưa inject sidecar (chưa có nhãn canonical-name) -> không đủ định danh
    chuẩn, không sinh evidence có ý nghĩa cho Tier 1."""
    reader = _online_boutique_reader()
    records = collect_config_evidence(reader, snapshot_id="snap_test")
    assert all(r.source_canonical_service != "no-sidecar-pod" for r in records)


def test_collect_config_evidence_envfrom_configmap():
    pod = _pod(
        "checkout-abc",
        labels={"security.istio.io/canonical-name": "checkoutservice"},
        sa="checkout-sa",
        containers=[ContainerSpec(name="server", env_from_configmap_names=["checkout-cm"])],
    )
    from tier1.config_collector import ConfigMapInfo

    reader = FakeK8sReader(
        pods=[pod],
        services=[ServiceInfo(name="emailservice", namespace=NS, selector={"app": "email"})],
        configmaps=[ConfigMapInfo(name="checkout-cm", namespace=NS, data={"EMAIL_SVC": "emailservice"})],
    )
    records = collect_config_evidence(reader, snapshot_id="snap_test")
    assert len(records) == 1
    assert records[0].destination_canonical_service == "emailservice"
    assert records[0].evidence_source == "envFrom"


def test_collect_config_evidence_namespaced_query():
    reader = _online_boutique_reader()
    records = collect_config_evidence(reader, snapshot_id="snap_test", namespaces=[NS])
    assert len(records) >= 1
