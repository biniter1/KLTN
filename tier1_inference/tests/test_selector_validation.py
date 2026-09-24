"""selector_validation.py — MockSelectorValidator (mục 3b, dùng cho demo/test không cần cluster)."""

from tier1.schema import DependencyEdgeResult, DependencyStatus, Scope, ServiceRef, TrafficClass
from tier1.selector_validation import MockSelectorValidator

from .conftest import make_identity_key


def _result(dest="productcatalogservice", ref=None) -> DependencyEdgeResult:
    key = make_identity_key(dest=dest)
    return DependencyEdgeResult(
        identity_key=key,
        destination_service_ref=ref,
        scope=Scope.IN_CLUSTER,
        traffic_class=TrafficClass.BUSINESS,
        count=100,
        distinct_days=5,
        error_ratio=0.0,
        unstable=False,
        config_finding=None,
        score=0.9,
        dependency_status=DependencyStatus.OBSERVED_STRONG,
        snapshot_id="snap_test",
    )


def test_fails_when_no_service_ref():
    validator = MockSelectorValidator()
    result = _result(ref=None)
    v = validator.validate(result)
    assert v.passed is False
    assert "destination_service_ref" in v.reason


def test_passes_for_normal_service():
    validator = MockSelectorValidator()
    ref = ServiceRef("productcatalogservice", "default")
    result = _result(dest="productcatalogservice", ref=ref)
    v = validator.validate(result)
    assert v.passed is True
    assert v.pod_count == 1
    assert v.overlapping_services == []


def test_fails_for_service_in_failing_list():
    validator = MockSelectorValidator(failing_services={"redis-cart"})
    ref = ServiceRef("redis-cart", "default")
    result = _result(dest="redis-cart", ref=ref)
    v = validator.validate(result)
    assert v.passed is False
    assert "redis-cart" in v.reason


def test_failing_list_does_not_affect_other_services():
    validator = MockSelectorValidator(failing_services={"redis-cart"})
    ref = ServiceRef("productcatalogservice", "default")
    result = _result(dest="productcatalogservice", ref=ref)
    v = validator.validate(result)
    assert v.passed is True
