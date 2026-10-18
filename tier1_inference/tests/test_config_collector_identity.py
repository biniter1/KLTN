"""config_collector/identity.py — bước [5e] CHUẨN HOÁ IDENTITY
(CONFIG_EVIDENCE_COLLECTOR.md mục 4.4e)."""

from __future__ import annotations

from tier1.config_collector.identity import (
    canonical_service_of,
    distinct_identities,
    resolve_identity,
    service_account_of,
)
from tier1.config_collector.k8s_types import PodInfo

NS = "default"


def _pod(name, labels=None, sa="default") -> PodInfo:
    return PodInfo(name=name, namespace=NS, service_account_name=sa, labels=labels or {})


def test_canonical_service_prefers_istio_label():
    pod = _pod("checkoutservice-abc123", labels={
        "service.istio.io/canonical-name": "checkoutservice",
        "app.kubernetes.io/name": "wrong",
        "app": "also-wrong",
    })
    assert canonical_service_of(pod) == "checkoutservice"


def test_canonical_service_accepts_legacy_istio_label_name():
    pod = _pod("x", labels={"security.istio.io/canonical-name": "legacy-name"})
    assert canonical_service_of(pod) == "legacy-name"


def test_canonical_service_falls_back_to_app_kubernetes_io_name():
    pod = _pod("x", labels={"app.kubernetes.io/name": "cartservice", "app": "wrong"})
    assert canonical_service_of(pod) == "cartservice"


def test_canonical_service_falls_back_to_app_label():
    pod = _pod("x", labels={"app": "cartservice"})
    assert canonical_service_of(pod) == "cartservice"


def test_canonical_service_falls_back_to_workload_name_when_no_labels():
    pod = _pod("raw-workload-name")
    assert canonical_service_of(pod) == "raw-workload-name"


def test_service_account_default_is_default():
    pod = _pod("x", sa="default")
    assert service_account_of(pod) == "default"


def test_service_account_custom():
    pod = _pod("x", sa="custom-sa")
    assert service_account_of(pod) == "custom-sa"


def test_resolve_identity_combines_all_fields():
    pod = _pod("checkoutservice-abc", labels={"app": "checkoutservice"}, sa="checkoutservice")
    ident = resolve_identity(pod)
    assert ident.namespace == NS
    assert ident.canonical_service == "checkoutservice"
    assert ident.service_account == "checkoutservice"
    assert ident.workload_name == "checkoutservice-abc"


def test_is_same_as_compares_namespace_and_canonical_service_only():
    a = resolve_identity(_pod("a", labels={"app": "svc"}, sa="sa-a"))
    b = resolve_identity(_pod("b", labels={"app": "svc"}, sa="sa-b"))
    assert a.is_same_as(b) is True  # SA khác nhau không ảnh hưởng


def test_is_same_as_false_for_different_namespace():
    pod_a = PodInfo(name="a", namespace="ns1", service_account_name="default", labels={"app": "svc"})
    pod_b = PodInfo(name="b", namespace="ns2", service_account_name="default", labels={"app": "svc"})
    a, b = resolve_identity(pod_a), resolve_identity(pod_b)
    assert a.is_same_as(b) is False


def test_distinct_identities_merges_replicas_of_same_workload():
    pods = [
        _pod("cartservice-aaa", labels={"app": "cartservice"}),
        _pod("cartservice-bbb", labels={"app": "cartservice"}),
    ]
    identities = distinct_identities(pods)
    assert len(identities) == 1
    assert identities[0].canonical_service == "cartservice"


def test_distinct_identities_keeps_different_workloads_separate_selector_overlap():
    pods = [
        _pod("cartservice-aaa", labels={"app": "cartservice"}),
        _pod("legacy-cart-aaa", labels={"app": "legacy-cart"}),
    ]
    identities = distinct_identities(pods)
    assert len(identities) == 2
    names = {i.canonical_service for i in identities}
    assert names == {"cartservice", "legacy-cart"}
