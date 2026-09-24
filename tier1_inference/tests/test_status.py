"""status.py — 5-state derivation + hysteresis bất đối xứng — TIER1_DESIGN.md mục 5, 5b."""

import pytest

from tier1.aggregation import TrafficAggregate
from tier1.config import ScoringConfig
from tier1.schema import ConfigFinding, DependencyStatus, IdentityKey, TrafficClass
from tier1.status import (
    apply_hysteresis,
    compute_unstable_classification,
    derive_status_observed,
    derive_status_potential_only,
    downgrade_to_manual_selector,
    is_class_ambiguous,
)

STRONG = DependencyStatus.OBSERVED_STRONG
WEAK = DependencyStatus.OBSERVED_WEAK


def _cfg():
    return ScoringConfig(theta_promote=0.68, theta_demote=0.58, theta_strong=0.6)


def _agg(count=100, class_counts=None):
    key = IdentityKey("default", "frontend", "frontend-sa", "default", "svc", "GRPC", 3550)
    agg = TrafficAggregate(identity_key=key, destination_service_ref=None)
    agg.count = count
    if class_counts:
        for cls, c in class_counts.items():
            agg.class_counts[cls] = c
    else:
        agg.class_counts[TrafficClass.BUSINESS] = count
    return agg


# --------------------------------------------------------------------------- apply_hysteresis

@pytest.mark.parametrize("score,expected", [(0.68, STRONG), (0.679, WEAK), (0.9, STRONG), (0.0, WEAK)])
def test_hysteresis_from_not_strong_needs_promote_threshold(score, expected):
    cfg = _cfg()
    assert apply_hysteresis(score, previous_status=None, config=cfg) == expected


@pytest.mark.parametrize("score,expected", [(0.58, STRONG), (0.579, WEAK), (0.9, STRONG)])
def test_hysteresis_from_strong_needs_to_drop_below_demote(score, expected):
    cfg = _cfg()
    assert apply_hysteresis(score, previous_status=STRONG, config=cfg) == expected


def test_hysteresis_dead_zone_keeps_previous_state():
    cfg = _cfg()
    dead_zone_score = 0.62  # nằm giữa theta_demote=0.58 và theta_promote=0.68
    assert apply_hysteresis(dead_zone_score, previous_status=STRONG, config=cfg) == STRONG
    assert apply_hysteresis(dead_zone_score, previous_status=WEAK, config=cfg) == WEAK
    assert apply_hysteresis(dead_zone_score, previous_status=None, config=cfg) == WEAK


def test_hysteresis_weak_previous_also_needs_promote():
    cfg = _cfg()
    assert apply_hysteresis(0.60, previous_status=WEAK, config=cfg) == WEAK
    assert apply_hysteresis(0.70, previous_status=WEAK, config=cfg) == STRONG


# --------------------------------------------------------------------------- is_class_ambiguous

def test_class_ambiguous_at_or_above_threshold(config):
    agg = _agg(count=100, class_counts={TrafficClass.UNKNOWN: 50, TrafficClass.BUSINESS: 50})
    assert is_class_ambiguous(agg, config) is True  # đúng threshold mặc định 0.5


def test_class_not_ambiguous_below_threshold(config):
    agg = _agg(count=100, class_counts={TrafficClass.UNKNOWN: 49, TrafficClass.BUSINESS: 51})
    assert is_class_ambiguous(agg, config) is False


# --------------------------------------------------------------------------- derive_status_observed

def test_derive_observed_ambiguous_class_forces_unknown(config):
    agg = _agg(class_counts={TrafficClass.UNKNOWN: 100})
    decision = derive_status_observed(agg, score=0.99, previous_status=None, config=config)
    assert decision.status == DependencyStatus.UNKNOWN
    assert decision.escalate_to_tier2 is True
    assert "UNKNOWN" in decision.escalation_reason


def test_derive_observed_strong(config):
    agg = _agg()
    cfg = _cfg()
    decision = derive_status_observed(agg, score=0.9, previous_status=None, config=cfg)
    assert decision.status == STRONG
    assert decision.escalate_to_tier2 is False
    assert decision.escalation_reason is None


def test_derive_observed_weak_escalates(config):
    agg = _agg()
    cfg = _cfg()
    decision = derive_status_observed(agg, score=0.3, previous_status=None, config=cfg)
    assert decision.status == WEAK
    assert decision.escalate_to_tier2 is True
    assert "OBSERVED_WEAK" in decision.escalation_reason


def test_derive_observed_count_floor_blocks_strong_despite_high_score():
    cfg = ScoringConfig(theta_promote=0.68, theta_demote=0.58, count_floor=(10, 2))
    agg = _agg(count=3)  # dưới count_floor (10)
    decision = derive_status_observed(agg, score=0.99, previous_status=None, config=cfg)
    assert decision.status == WEAK  # không được phép lên STRONG dù score rất cao
    assert decision.escalate_to_tier2 is True


def test_derive_observed_count_floor_disabled_by_default_allows_strong():
    cfg = ScoringConfig(theta_promote=0.68, theta_demote=0.58)  # count_floor=None mặc định
    agg = _agg(count=1)  # traffic rất thấp nhưng count_floor tắt -> không bị chặn
    decision = derive_status_observed(agg, score=0.99, previous_status=None, config=cfg)
    assert decision.status == STRONG


# --------------------------------------------------------------------------- derive_status_potential_only

def test_potential_only_clean_match_goes_to_human_review_not_escalate():
    findings = [ConfigFinding.FOUND_EMBEDDED]
    decision = derive_status_potential_only(findings)
    assert decision.status == DependencyStatus.NOT_OBSERVED
    assert decision.escalate_to_tier2 is False
    assert decision.needs_human_review is True


def test_potential_only_structural_also_counts_as_clean():
    decision = derive_status_potential_only([ConfigFinding.FOUND_STRUCTURAL])
    assert decision.needs_human_review is True
    assert decision.escalate_to_tier2 is False


def test_potential_only_mixed_findings_any_clean_match_wins():
    findings = [ConfigFinding.NOT_FOUND, ConfigFinding.FOUND_EMBEDDED, ConfigFinding.FOUND_UNRESOLVED]
    decision = derive_status_potential_only(findings)
    assert decision.escalate_to_tier2 is False
    assert decision.needs_human_review is True


def test_potential_only_blind_escalates():
    findings = [ConfigFinding.FOUND_UNRESOLVED, ConfigFinding.NOT_FOUND]
    decision = derive_status_potential_only(findings)
    assert decision.status == DependencyStatus.NOT_OBSERVED
    assert decision.escalate_to_tier2 is True
    assert decision.needs_human_review is False
    assert "mù hoàn toàn" in decision.escalation_reason


# --------------------------------------------------------------------------- compute_unstable_classification

def test_unstable_classification_threshold(config):
    assert compute_unstable_classification(3, config) is True
    assert compute_unstable_classification(2, config) is False
    assert compute_unstable_classification(0, config) is False


# --------------------------------------------------------------------------- downgrade_to_manual_selector

def test_downgrade_to_manual_selector():
    decision = downgrade_to_manual_selector("selector rỗng")
    assert decision.status == DependencyStatus.NEEDS_MANUAL_SELECTOR
    assert decision.escalate_to_tier2 is False
    assert decision.needs_human_review is True
