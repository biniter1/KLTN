"""discover_candidates() — TIER1_DESIGN.md mục 1: C = C_observed ∪ C_potential."""

from tier1.aggregation import TrafficAggregate
from tier1.candidates import discover_candidates
from tier1.schema import ConfigFinding

from .conftest import make_config_evidence, make_identity_key


def _fake_aggregate(identity_key, count=10):
    return TrafficAggregate(identity_key=identity_key, destination_service_ref=None, count=count)


def test_observed_only_pair(config):
    key = make_identity_key(dest="productcatalogservice")
    aggs = {key: _fake_aggregate(key)}
    candidates = discover_candidates(aggs, [])
    assert len(candidates) == 1
    cand = next(iter(candidates.values()))
    assert cand.is_observed
    assert not cand.is_potential_only


def test_potential_only_pair_no_traffic(config):
    ev = make_config_evidence(dest="emailservice", finding=ConfigFinding.FOUND_EMBEDDED)
    candidates = discover_candidates({}, [ev])
    assert len(candidates) == 1
    cand = next(iter(candidates.values()))
    assert not cand.is_observed
    assert cand.is_potential_only


def test_count_zero_traffic_does_not_count_as_observed(config):
    """mục 1: C_observed chỉ gồm cặp count>0 — aggregate count=0 (dù có mặt trong
    traffic_aggregates) không được tính là 'đã quan sát'."""
    key = make_identity_key(dest="rarely-called")
    aggs = {key: _fake_aggregate(key, count=0)}
    ev = make_config_evidence(dest="rarely-called", finding=ConfigFinding.FOUND_UNRESOLVED)
    candidates = discover_candidates(aggs, [ev])
    cand = next(iter(candidates.values()))
    assert not cand.is_observed
    assert cand.is_potential_only


def test_pair_both_observed_and_has_config_evidence(config):
    key = make_identity_key(dest="productcatalogservice")
    aggs = {key: _fake_aggregate(key, count=500)}
    ev = make_config_evidence(dest="productcatalogservice")
    candidates = discover_candidates(aggs, [ev])
    cand = next(iter(candidates.values()))
    assert cand.is_observed
    assert not cand.is_potential_only  # có traffic -> không phải "chỉ tiềm năng"
    assert len(cand.config_evidence) == 1


def test_multiple_protocol_port_variants_grouped_under_same_pair(config):
    """2 identity_key khác protocol/port nhưng cùng (source,dest) -> cùng 1 Candidate.pair_key,
    nhưng observed dict có 2 entry riêng (đúng thiết kế mục 1: C_potential không phân biệt
    protocol/port, nhưng C_observed thì có)."""
    key_grpc = make_identity_key(dest="svc", protocol="GRPC", port=8080)
    key_tcp = make_identity_key(dest="svc", protocol="TCP", port=9090)
    aggs = {key_grpc: _fake_aggregate(key_grpc, count=5), key_tcp: _fake_aggregate(key_tcp, count=5)}
    candidates = discover_candidates(aggs, [])
    assert len(candidates) == 1
    cand = next(iter(candidates.values()))
    assert len(cand.observed) == 2


def test_unrelated_pairs_are_not_merged(config):
    key1 = make_identity_key(dest="svcA")
    key2 = make_identity_key(dest="svcB")
    aggs = {key1: _fake_aggregate(key1), key2: _fake_aggregate(key2)}
    candidates = discover_candidates(aggs, [])
    assert len(candidates) == 2
