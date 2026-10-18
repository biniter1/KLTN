"""config_collector/validate.py — bước [5] VALIDATION (CONFIG_EVIDENCE_COLLECTOR.md
mục 4.4) -- bao phủ các rẽ nhánh (a)-(g), dùng FakeK8sReader, không cần cluster thật."""

from __future__ import annotations

from tier1.config_collector.extract import LLMReference
from tier1.config_collector.funnel import FunnelItem
from tier1.config_collector.k8s_types import (
    EndpointsInfo,
    FakeK8sReader,
    PodInfo,
    ServiceEntryInfo,
    ServiceInfo,
    ServicePortInfo,
    VirtualServiceInfo,
)
from tier1.config_collector.validate import (
    ClusterSnapshot,
    EvidenceGroup,
    build_cluster_snapshot,
    classify_host_shape,
    finalize_evidence,
    is_loopback_host,
    resolve_host,
    resolve_port,
    validate_reference,
)
from tier1.schema import ConfigFinding, Scope

NS = "default"


def _pod(name, labels=None, is_running=True, sa="default", namespace=NS) -> PodInfo:
    return PodInfo(name=name, namespace=namespace, service_account_name=sa, labels=labels or {}, is_running=is_running)


def _item(item_id, value, key="ADDR", source_kind="env", container="main", workload="src") -> FunnelItem:
    return FunnelItem(
        item_id=item_id, workload=workload, namespace=NS, container=container, container_type="app",
        source_kind=source_kind, key=key, value=value, redacted_value=value, funnel_status="KEPT",
        source_ref=None, rendered_block=f"<config_item>{value}</config_item>",
    )


def _ref(item_id="i0000", quote="cartservice:7070", host="cartservice", port=7070, **kwargs) -> LLMReference:
    return LLMReference(item_id=item_id, quote=quote, host=host, port=port, **kwargs)


def _snapshot(pods=(), services=(), endpoints=(), virtualservices=(), serviceentries=(), namespaces=None) -> ClusterSnapshot:
    reader = FakeK8sReader(
        pods=list(pods), services=list(services), endpoints=list(endpoints),
        virtualservices=list(virtualservices), serviceentries=list(serviceentries), namespaces=namespaces,
    )
    return build_cluster_snapshot(reader)


SOURCE_POD = _pod("checkoutservice-abc", labels={"app": "checkoutservice"})


# ---------------------------------------------------------------------------
# (a) grounding
# ---------------------------------------------------------------------------


def test_grounded_reference_passes():
    items = {"i0000": _item("i0000", "cartservice:7070")}
    results = validate_reference(_ref(), items, SOURCE_POD, _snapshot())
    assert results[0].status != "REJECTED_UNGROUNDED"


def test_quote_not_in_item_text_is_ungrounded():
    items = {"i0000": _item("i0000", "totally-different-value")}
    results = validate_reference(_ref(), items, SOURCE_POD, _snapshot())
    assert results[0].status == "REJECTED_UNGROUNDED"


def test_host_not_substring_of_quote_is_ungrounded():
    items = {"i0000": _item("i0000", "cartservice:7070")}
    ref = _ref(quote="cartservice:7070", host="totally-other-host")
    results = validate_reference(ref, items, SOURCE_POD, _snapshot())
    assert results[0].status == "REJECTED_UNGROUNDED"


def test_port_not_in_quote_without_port_item_is_ungrounded():
    items = {"i0000": _item("i0000", "cartservice (no port here)")}
    ref = _ref(quote="cartservice", host="cartservice", port=9999)
    results = validate_reference(ref, items, SOURCE_POD, _snapshot())
    assert results[0].status == "REJECTED_UNGROUNDED"


def test_split_host_port_grounds_via_port_item_id():
    items = {
        "i0000": _item("i0000", "cartservice", key="DB_HOST"),
        "i0001": _item("i0001", "7070", key="DB_PORT"),
    }
    ref = _ref(item_id="i0000", quote="cartservice", host="cartservice", port=7070,
               port_item_id="i0001", port_quote="7070")
    results = validate_reference(ref, items, SOURCE_POD, _snapshot())
    assert results[0].status != "REJECTED_UNGROUNDED"


def test_dropped_funnel_item_cannot_ground_anything():
    dropped = FunnelItem(
        item_id="i0000", workload="src", namespace=NS, container="main", container_type="app",
        source_kind="secret_ref", key="K", value="<redacted>", redacted_value="", funnel_status="SECRET_REF",
        source_ref=None, rendered_block="",
    )
    results = validate_reference(_ref(), {"i0000": dropped}, SOURCE_POD, _snapshot())
    assert results[0].status == "REJECTED_UNGROUNDED"


# ---------------------------------------------------------------------------
# (b) dạng host
# ---------------------------------------------------------------------------


def test_classify_host_shape_ip():
    assert classify_host_shape("10.0.0.5") == "IP"


def test_classify_host_shape_dns():
    assert classify_host_shape("cartservice.default.svc.cluster.local") == "DNS"


def test_classify_host_shape_invalid():
    assert classify_host_shape("not a host!!") == "INVALID"


def test_is_loopback_variants():
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is True
    assert is_loopback_host("cartservice") is False


def test_invalid_host_rejected():
    items = {"i0000": _item("i0000", "weird value: not a host!!")}
    ref = _ref(quote="not a host!!", host="not a host!!", port=None)
    results = validate_reference(ref, items, SOURCE_POD, _snapshot())
    assert results[0].status == "REJECTED_INVALID_HOST"


def test_loopback_host_dropped():
    items = {"i0000": _item("i0000", "localhost:5432")}
    ref = _ref(quote="localhost:5432", host="localhost", port=5432)
    results = validate_reference(ref, items, SOURCE_POD, _snapshot())
    assert results[0].status == "DROP_LOOPBACK"


# ---------------------------------------------------------------------------
# (c) phân giải host — Service có selector, chạy / chỉ có template / không có backend
# ---------------------------------------------------------------------------


def test_resolve_service_with_selector_matches_running_pod():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"},
                       ports=[ServicePortInfo(port=7070, target_port=7070)])
    snap = _snapshot(pods=[dest_pod], services=[svc])
    resolved = resolve_host("cartservice", NS, snap)
    assert resolved.outcome == "FOUND"
    assert resolved.scope == Scope.IN_CLUSTER
    assert len(resolved.pods) == 1
    assert resolved.flags == ()


def test_resolve_service_with_selector_falls_back_to_pod_template_flags_dest_not_running():
    dest_template = _pod("cronjob-worker", labels={"app": "cronjob-worker"}, is_running=False)
    svc = ServiceInfo(name="cronjob-worker", namespace=NS, selector={"app": "cronjob-worker"})
    snap = _snapshot(pods=[dest_template], services=[svc])
    resolved = resolve_host("cronjob-worker", NS, snap)
    assert resolved.outcome == "FOUND"
    assert "dest_not_running" in resolved.flags


def test_resolve_service_with_selector_no_backend_at_all():
    svc = ServiceInfo(name="ghost-svc", namespace=NS, selector={"app": "ghost"})
    snap = _snapshot(services=[svc])
    resolved = resolve_host("ghost-svc", NS, snap)
    assert resolved.outcome == "FOUND_UNRESOLVED"
    assert resolved.unresolved_reason == "NO_BACKEND"


def test_resolve_service_without_selector_endpoints_pod_refs():
    dest_pod = _pod("manual-backend", labels={})
    svc = ServiceInfo(name="manual-svc", namespace=NS, selector=None)
    ep = EndpointsInfo(service_name="manual-svc", namespace=NS, pod_refs=[(NS, "manual-backend")])
    snap = _snapshot(pods=[dest_pod], services=[svc], endpoints=[ep])
    resolved = resolve_host("manual-svc", NS, snap)
    assert resolved.outcome == "FOUND"
    assert resolved.scope == Scope.IN_CLUSTER
    assert len(resolved.pods) == 1


def test_resolve_service_without_selector_endpoints_external_ip():
    svc = ServiceInfo(name="onprem-db", namespace=NS, selector=None)
    ep = EndpointsInfo(service_name="onprem-db", namespace=NS, addresses=["203.0.113.5"])
    snap = _snapshot(services=[svc], endpoints=[ep])
    resolved = resolve_host("onprem-db", NS, snap)
    assert resolved.outcome == "FOUND"
    assert resolved.scope == Scope.EXTERNAL
    assert resolved.external_kind == "SELECTORLESS_ENDPOINTS"


def test_resolve_service_without_selector_no_endpoints_no_backend():
    svc = ServiceInfo(name="empty-svc", namespace=NS, selector=None)
    snap = _snapshot(services=[svc])
    resolved = resolve_host("empty-svc", NS, snap)
    assert resolved.outcome == "FOUND_UNRESOLVED"
    assert resolved.unresolved_reason == "NO_BACKEND"


def test_resolve_external_name_service():
    svc = ServiceInfo(name="legacy-db", namespace=NS, service_type="ExternalName", external_name="db.onprem.corp")
    snap = _snapshot(services=[svc])
    resolved = resolve_host("legacy-db", NS, snap)
    assert resolved.outcome == "FOUND"
    assert resolved.scope == Scope.EXTERNAL
    assert resolved.external_kind == "EXTERNAL_NAME"


# ---------------------------------------------------------------------------
# (c) phân giải host — IP
# ---------------------------------------------------------------------------


def test_resolve_ip_matching_cluster_ip_resolves_as_that_service():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"}, cluster_ip="10.96.0.5")
    snap = _snapshot(pods=[dest_pod], services=[svc])
    resolved = resolve_host("10.96.0.5", NS, snap)
    assert resolved.outcome == "FOUND"
    assert resolved.scope == Scope.IN_CLUSTER


def test_resolve_private_ip_not_matching_any_service_is_pod_ip_unstable():
    snap = _snapshot(services=[])
    resolved = resolve_host("10.244.1.23", NS, snap)
    assert resolved.outcome == "FOUND_UNRESOLVED"
    assert resolved.unresolved_reason == "POD_IP_UNSTABLE"


def test_resolve_public_ip_is_external_unregistered():
    snap = _snapshot(services=[])
    resolved = resolve_host("8.8.8.8", NS, snap)
    assert resolved.outcome == "FOUND_UNRESOLVED"
    assert resolved.scope == Scope.EXTERNAL
    assert resolved.unresolved_reason == "EXTERNAL_IP_UNREGISTERED"


# ---------------------------------------------------------------------------
# (c) phân giải host — DNS short name forms
# ---------------------------------------------------------------------------


def test_resolve_bare_short_name_in_source_namespace():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"})
    snap = _snapshot(pods=[dest_pod], services=[svc])
    resolved = resolve_host("cartservice", NS, snap)
    assert resolved.outcome == "FOUND"


def test_resolve_svc_dot_ns_form_requires_real_namespace():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"}, namespace="other-ns")
    svc = ServiceInfo(name="cartservice", namespace="other-ns", selector={"app": "cartservice"})
    snap = _snapshot(pods=[dest_pod], services=[svc], namespaces=["other-ns", NS])
    resolved = resolve_host("cartservice.other-ns", NS, snap)
    assert resolved.outcome == "FOUND"


def test_resolve_two_label_host_with_fake_namespace_is_not_treated_as_k8s_short_name():
    # "cartservice.fakeenv" -- "fakeenv" KHÔNG phải namespace có thật -> không parse
    # được thành dạng K8s, rơi xuống nhánh FQDN ngoài (>=2 label) -> EXTERNAL_UNREGISTERED.
    snap = _snapshot(services=[], namespaces=[NS])
    resolved = resolve_host("cartservice.fakeenv", NS, snap)
    assert resolved.unresolved_reason == "EXTERNAL_UNREGISTERED"
    assert resolved.scope == Scope.EXTERNAL


def test_resolve_svc_dot_ns_dot_svc_form():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"})
    snap = _snapshot(pods=[dest_pod], services=[svc])
    resolved = resolve_host("cartservice.default.svc", NS, snap)
    assert resolved.outcome == "FOUND"


def test_resolve_full_fqdn_form():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"})
    snap = _snapshot(pods=[dest_pod], services=[svc])
    resolved = resolve_host("cartservice.default.svc.cluster.local", NS, snap)
    assert resolved.outcome == "FOUND"


def test_resolve_valid_k8s_form_but_no_matching_service_is_service_not_found():
    snap = _snapshot(services=[], namespaces=[NS])
    resolved = resolve_host("ghost.default.svc.cluster.local", NS, snap)
    assert resolved.unresolved_reason == "SERVICE_NOT_FOUND"


def test_resolve_bare_name_not_matching_anything_is_no_match():
    snap = _snapshot(services=[])
    resolved = resolve_host("totallyrandomname", NS, snap)
    assert resolved.unresolved_reason == "NO_MATCH"
    assert resolved.scope == Scope.UNKNOWN


# ---------------------------------------------------------------------------
# (c) ServiceEntry / VirtualService
# ---------------------------------------------------------------------------


def test_resolve_serviceentry_host():
    se = ServiceEntryInfo(name="external-api", namespace=NS, hosts=["api.stripe.com"])
    snap = _snapshot(serviceentries=[se])
    resolved = resolve_host("api.stripe.com", NS, snap)
    assert resolved.outcome == "FOUND"
    assert resolved.scope == Scope.EXTERNAL
    assert resolved.external_kind == "SERVICE_ENTRY"


def test_resolve_virtualservice_follows_one_hop_and_flags_via_virtualservice():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"})
    vs = VirtualServiceInfo(name="cart-vs", namespace=NS, hosts=["cart.example.com"], destination_host="cartservice")
    snap = _snapshot(pods=[dest_pod], services=[svc], virtualservices=[vs])
    resolved = resolve_host("cart.example.com", NS, snap)
    assert resolved.outcome == "FOUND"
    assert "via_virtualservice" in resolved.flags


# ---------------------------------------------------------------------------
# (d) port
# ---------------------------------------------------------------------------


def test_resolve_port_matches_by_number():
    svc = ServiceInfo(name="s", namespace=NS, ports=[ServicePortInfo(port=80, target_port=8080, protocol="TCP")])
    service_port, target_port, protocol, port_unknown, flags = resolve_port(svc, 80)
    assert (service_port, target_port, port_unknown, flags) == (80, 8080, False, ())
    assert protocol == "TCP"


def test_resolve_port_mismatch_keeps_given_port_flagged():
    svc = ServiceInfo(name="s", namespace=NS, ports=[ServicePortInfo(port=80, target_port=8080)])
    service_port, target_port, protocol, port_unknown, flags = resolve_port(svc, 9999)
    assert service_port == 9999 and target_port == 9999 and flags == ("port_mismatch",)


def test_resolve_port_inferred_when_single_port_and_no_given_port():
    svc = ServiceInfo(name="s", namespace=NS, ports=[ServicePortInfo(port=80, target_port=8080)])
    service_port, target_port, protocol, port_unknown, flags = resolve_port(svc, None)
    assert (service_port, target_port, port_unknown, flags) == (80, 8080, False, ("port_inferred",))


def test_resolve_port_unknown_when_multiple_ports_and_no_given_port():
    svc = ServiceInfo(name="s", namespace=NS, ports=[
        ServicePortInfo(port=80, target_port=8080), ServicePortInfo(port=443, target_port=8443),
    ])
    service_port, target_port, protocol, port_unknown, flags = resolve_port(svc, None)
    assert port_unknown is True and service_port is None and target_port is None


def test_resolve_port_no_service_passes_through_given_port():
    service_port, target_port, protocol, port_unknown, flags = resolve_port(None, 5432)
    assert (service_port, target_port, port_unknown) == (5432, 5432, False)


# ---------------------------------------------------------------------------
# (f) tự tham chiếu
# ---------------------------------------------------------------------------


def test_self_reference_dropped():
    source = _pod("checkoutservice-abc", labels={"app": "checkoutservice"})
    svc = ServiceInfo(name="checkoutservice", namespace=NS, selector={"app": "checkoutservice"})
    snap = _snapshot(pods=[source], services=[svc])
    items = {"i0000": _item("i0000", "checkoutservice:5050")}
    ref = _ref(quote="checkoutservice:5050", host="checkoutservice", port=5050)
    results = validate_reference(ref, items, source, snap)
    assert results[0].status == "DROP_SELF"


# ---------------------------------------------------------------------------
# selector_overlap — nhiều workload khác nhau cùng khớp 1 selector
# ---------------------------------------------------------------------------


def test_selector_matching_multiple_distinct_workloads_flags_overlap_and_yields_multiple_results():
    dest_a = _pod("svc-a-1", labels={"tier": "backend"})
    dest_b = _pod("svc-b-1", labels={"tier": "backend"})
    svc = ServiceInfo(name="shared-svc", namespace=NS, selector={"tier": "backend"})
    snap = _snapshot(pods=[dest_a, dest_b], services=[svc])
    items = {"i0000": _item("i0000", "shared-svc:80")}
    ref = _ref(quote="shared-svc:80", host="shared-svc", port=80)
    results = validate_reference(ref, items, SOURCE_POD, snap)
    assert len(results) == 2
    assert all("selector_overlap" in r.flags for r in results)
    assert {r.destination.canonical_service for r in results} == {"svc-a-1", "svc-b-1"}


# ---------------------------------------------------------------------------
# config_finding: STRUCTURAL vs EMBEDDED
# ---------------------------------------------------------------------------


def test_structural_when_quote_is_entire_value():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"},
                       ports=[ServicePortInfo(port=7070, target_port=7070)])
    snap = _snapshot(pods=[dest_pod], services=[svc])
    items = {"i0000": _item("i0000", "cartservice:7070")}
    ref = _ref(quote="cartservice:7070", host="cartservice", port=7070)
    results = validate_reference(ref, items, SOURCE_POD, snap)
    assert results[0].status == "FOUND_STRUCTURAL"


def test_embedded_when_quote_is_partial_value():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"},
                       ports=[ServicePortInfo(port=7070, target_port=7070)])
    snap = _snapshot(pods=[dest_pod], services=[svc])
    items = {"i0000": _item("i0000", "jdbc:postgresql://cartservice:7070/db?password=x")}
    ref = _ref(quote="cartservice:7070", host="cartservice", port=7070)
    results = validate_reference(ref, items, SOURCE_POD, snap)
    assert results[0].status == "FOUND_EMBEDDED"


# ---------------------------------------------------------------------------
# (g) finalize_evidence — khử trùng + ưu tiên nhãn
# ---------------------------------------------------------------------------


def test_finalize_evidence_merges_same_pair_keeps_all_evidence_refs():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"},
                       ports=[ServicePortInfo(port=7070, target_port=7070)])
    snap = _snapshot(pods=[dest_pod], services=[svc])

    items = {
        "i0000": _item("i0000", "cartservice:7070", key="A"),
        "i0001": _item("i0001", "jdbc://cartservice:7070/x", key="B"),
    }
    results = []
    results += validate_reference(_ref(item_id="i0000", quote="cartservice:7070", host="cartservice", port=7070),
                                   items, SOURCE_POD, snap)
    results += validate_reference(_ref(item_id="i0001", quote="cartservice:7070", host="cartservice", port=7070),
                                   items, SOURCE_POD, snap)

    groups = finalize_evidence(results)
    assert len(groups) == 1
    group = groups[0]
    # 1 trong 2 evidence là FOUND_STRUCTURAL (toàn bộ giá trị "cartservice:7070" của i0000)
    assert group.config_finding == ConfigFinding.FOUND_STRUCTURAL
    assert len(group.evidence_refs) == 2


def test_finalize_evidence_excludes_dropped_and_rejected():
    dest_pod = _pod("cartservice-xyz", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"})
    snap = _snapshot(pods=[dest_pod], services=[svc])

    items = {
        "i0000": _item("i0000", "cartservice:7070"),
        "i0001": _item("i0001", "localhost:1234"),
    }
    found = validate_reference(_ref(item_id="i0000", quote="cartservice:7070", host="cartservice", port=7070),
                                items, SOURCE_POD, snap)
    dropped = validate_reference(_ref(item_id="i0001", quote="localhost:1234", host="localhost", port=1234),
                                  items, SOURCE_POD, snap)

    groups = finalize_evidence(found + dropped)
    assert len(groups) == 1  # chỉ cặp FOUND_* mới thành evidence


def test_finalize_evidence_unresolved_kept_as_raw_host_pair():
    snap = _snapshot(services=[])
    items = {"i0000": _item("i0000", "unknownhost:1234")}
    results = validate_reference(_ref(item_id="i0000", quote="unknownhost:1234", host="unknownhost", port=1234),
                                  items, SOURCE_POD, snap)
    groups = finalize_evidence(results)
    assert len(groups) == 1
    assert groups[0].config_finding == ConfigFinding.FOUND_UNRESOLVED
    assert groups[0].destination is None
    assert groups[0].raw_host == "unknownhost"
