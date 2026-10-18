"""dns_external.py — scope IN_CLUSTER/EXTERNAL/UNKNOWN (mục 8, thứ tự chốt 15/10).

DNS không còn quyết ở đây (bỏ `is_dns_edge`) — xem test_classification.py và
test_pipeline.py::test_dns_split_by_traffic_class."""

import tier1.dns_external as dns_external
from tier1.dns_external import classify_scope
from tier1.schema import Scope, ServiceRef

from .conftest import make_identity_key


def test_is_dns_edge_removed():
    assert not hasattr(dns_external, "is_dns_edge")


def test_scope_in_cluster_when_service_ref_present():
    key = make_identity_key(dest="productcatalogservice")
    ref = ServiceRef("productcatalogservice", "default")
    assert classify_scope(key, ref) == Scope.IN_CLUSTER


def test_scope_external_when_passthrough_flag_even_with_service_ref():
    key = make_identity_key(dest="unknown")
    ref = ServiceRef("something", "default")
    assert classify_scope(key, ref, {"external_unregistered"}) == Scope.EXTERNAL


def test_scope_external_when_serviceentry_host():
    key = make_identity_key(dest="api-stripe")
    ref = ServiceRef("api.stripe.com", "default")  # Istio ghi host ServiceEntry vào service_name
    assert classify_scope(key, ref, ["serviceentry_host"]) == Scope.EXTERNAL


def test_scope_external_when_fqdn_and_no_ref():
    key = make_identity_key(dest="api.stripe.com")
    assert classify_scope(key, None) == Scope.EXTERNAL


def test_scope_unknown_when_no_ref_and_not_fqdn():
    key = make_identity_key(dest="shipping-backend")  # tên trần, không dấu chấm, không service_ref
    assert classify_scope(key, None) == Scope.UNKNOWN
