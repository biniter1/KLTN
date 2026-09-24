"""dns_external.py — DNS auto-allow (mục 7) + scope IN_CLUSTER/EXTERNAL/UNKNOWN (mục 8)."""

from tier1.dns_external import classify_scope, is_dns_edge
from tier1.schema import Scope, ServiceRef

from .conftest import make_identity_key


def test_is_dns_edge_by_port(config):
    key = make_identity_key(port=53, protocol="TCP")
    assert is_dns_edge(key, config) is True


def test_is_dns_edge_by_protocol_name(config):
    key = make_identity_key(port=12345, protocol="DNS")
    assert is_dns_edge(key, config) is True


def test_not_dns_edge(config):
    key = make_identity_key(port=8080, protocol="HTTP")
    assert is_dns_edge(key, config) is False


def test_scope_in_cluster_when_service_ref_present():
    key = make_identity_key(dest="productcatalogservice")
    ref = ServiceRef("productcatalogservice", "default")
    assert classify_scope(key, ref) == Scope.IN_CLUSTER


def test_scope_external_when_fqdn_and_no_ref():
    key = make_identity_key(dest="api.stripe.com")
    assert classify_scope(key, None) == Scope.EXTERNAL


def test_scope_unknown_when_no_ref_and_not_fqdn():
    key = make_identity_key(dest="shipping-backend")  # tên trần, không dấu chấm, không service_ref
    assert classify_scope(key, None) == Scope.UNKNOWN
