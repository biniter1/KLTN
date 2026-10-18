"""KubernetesSelectorValidator với CoreV1Api giả — sửa lỗi nhãn canonical (L1) và đọc
Service/Pod 1 lần mỗi namespace (TIER1_DESIGN.md mục 3b, chốt 15/10/2026)."""

from types import SimpleNamespace as NS_

from tier1.schema import DependencyEdgeResult, DependencyStatus, Scope, ServiceRef, TrafficClass
from tier1.selector_validation import KubernetesSelectorValidator, pod_canonical_service

from .conftest import make_identity_key


def _pod(name, labels, owner=None):
    refs = [NS_(name=owner)] if owner else None
    return NS_(metadata=NS_(name=name, labels=labels, owner_references=refs))


def _svc(name, selector):
    return NS_(metadata=NS_(name=name), spec=NS_(selector=selector))


class _Core:
    def __init__(self, services, pods):
        self.services, self.pods, self.calls = services, pods, 0

    def list_namespaced_service(self, ns):
        self.calls += 1
        return NS_(items=self.services)

    def list_namespaced_pod(self, ns):
        self.calls += 1
        return NS_(items=self.pods)


def _edge(dest):
    return DependencyEdgeResult(
        identity_key=make_identity_key(dest=dest), destination_service_ref=ServiceRef(dest, "default"),
        scope=Scope.IN_CLUSTER, traffic_class=TrafficClass.BUSINESS, count=10, distinct_days=7,
        error_ratio=0.0, unstable=False, config_finding=None, score=0.9,
        dependency_status=DependencyStatus.OBSERVED_STRONG, snapshot_id="s",
    )


def test_canonical_derived_like_istio_when_pod_has_no_canonical_label():
    assert pod_canonical_service({"app": "cartservice"}, None, "cartservice-1") == "cartservice"
    assert pod_canonical_service({"service.istio.io/canonical-name": "cart"}, None, "x") == "cart"
    assert pod_canonical_service({"pod-template-hash": "abc"}, "frontend-abc", "frontend-abc-1") == "frontend"


def test_pod_without_canonical_label_passes_step_4():
    """Lỗi cũ: đọc `security.istio.io/canonical-name` -> pod chỉ có `app` bị coi là lệch."""
    core = _Core([_svc("cartservice", {"app": "cartservice"})],
                 [_pod("cartservice-7d9f-x", {"app": "cartservice", "pod-template-hash": "7d9f"}, "cartservice-7d9f")])
    assert KubernetesSelectorValidator(core_api=core).validate(_edge("cartservice")).passed


def test_overlap_and_wrong_workload_still_rejected():
    pods = [_pod("cart-1", {"app": "cartservice", "tier": "be"}), _pod("other-1", {"app": "other", "tier": "be"})]
    core = _Core([_svc("cartservice", {"tier": "be"}), _svc("other", {"app": "other"})], pods)
    res = KubernetesSelectorValidator(core_api=core).validate(_edge("cartservice"))
    assert not res.passed and res.overlapping_services == ["other"]


def test_namespace_read_once_for_many_edges():
    core = _Core([_svc("a", {"app": "a"}), _svc("b", {"app": "b"})], [_pod("a-1", {"app": "a"}), _pod("b-1", {"app": "b"})])
    v = KubernetesSelectorValidator(core_api=core)
    assert v.validate(_edge("a")).passed and v.validate(_edge("b")).passed
    assert core.calls == 2  # 1 lần list service + 1 lần list pod cho cả namespace
