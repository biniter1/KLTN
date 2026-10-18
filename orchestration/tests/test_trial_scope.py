"""trial_scope.py — tách 1 virtual trial khỏi namespace dùng chung theo pair_key + owner_map."""

from __future__ import annotations

import json

import pytest

from orchestration.trial_scope import load_trial_scope
from tier1.schema import ConfigEvidenceRecord, ConfigFinding, Scope

from .conftest import NS, pk


def _unresolved(src: str, raw_host: str) -> ConfigEvidenceRecord:
    return ConfigEvidenceRecord(
        source_namespace=NS, source_canonical_service=src, source_service_account=src,
        destination_namespace=None, destination_canonical_service=None, destination_service_account=None,
        protocol=None, port=None, port_unknown=True, config_finding=ConfigFinding.FOUND_UNRESOLVED,
        evidence_source="env", scope=Scope.UNKNOWN, snapshot_id="s", collected_at="t", raw_host=raw_host)


@pytest.fixture
def scope(infra_files):
    return load_trial_scope("trial-01", NS, infra_files["ground_truth_dir"], infra_files["owner_map_path"])


def test_real_online_boutique_edges_owned_by_every_trial(infra_files, scope):
    other = load_trial_scope("trial-02", NS, infra_files["ground_truth_dir"], infra_files["owner_map_path"])
    assert scope.owns(pk("frontend", "cartservice")) and other.owns(pk("frontend", "cartservice"))


def test_injected_edge_owned_only_by_its_owners(scope):
    assert scope.owns(pk("adservice", "paymentservice"))
    assert not scope.owns(pk("shippingservice", "emailservice"))
    assert not scope.owns(pk("loadgenerator", "frontend"))


def test_unresolved_config_matched_by_nonexistent_suffix(scope):
    assert scope.owns_config_evidence(_unresolved("paymentservice", "adservice-nonexistent"))
    assert scope.owns_config_evidence(_unresolved("paymentservice", "adservice-nonexistent.shared-eval.svc"))
    assert not scope.owns_config_evidence(_unresolved("paymentservice", "redis-nonexistent"))
    assert not scope.owns_config_evidence(_unresolved("frontend", "adservice-nonexistent"))  # sai nguồn


def test_missing_owner_map_or_unowned_trial_is_explicit_error(infra_files, tmp_path):
    with pytest.raises(FileNotFoundError, match="owner_map"):
        load_trial_scope("trial-01", NS, infra_files["ground_truth_dir"], tmp_path / "missing.json")
    (infra_files["ground_truth_dir"] / "trial-07.json").write_text(json.dumps({}))
    with pytest.raises(ValueError, match="trial-07"):
        load_trial_scope("trial-07", NS, infra_files["ground_truth_dir"], infra_files["owner_map_path"])


def test_malformed_pair_key_rejected(infra_files):
    infra_files["owner_map_path"].write_text(json.dumps({"a|b|c": ["trial-01"]}))
    with pytest.raises(ValueError, match="ns|src|ns|dst"):
        load_trial_scope("trial-01", NS, infra_files["ground_truth_dir"], infra_files["owner_map_path"])
