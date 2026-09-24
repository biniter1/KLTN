from tier2_agent.models import (
    EvidenceRow,
    FinalStatus,
    HypothesisStatus,
    HypothesisView,
)
from tier2_agent.validation import derive_final_statuses, derive_hypothesis_status, distinct_evidence_sources


def _ev(**kwargs):
    base = dict(
        evidence_id="E1",
        run_id="r",
        iteration=1,
        hypothesis_id="H1",
        tool_name="read_source_code",
        tool_input={},
        tool_output="x",
        stance=None,
        status="OK",
    )
    base.update(kwargs)
    return EvidenceRow(**base)


def test_hypothesis_status_proposed_when_no_evidence():
    assert derive_hypothesis_status([]) == HypothesisStatus.PROPOSED


def test_hypothesis_status_supported():
    assert derive_hypothesis_status([_ev(stance="support")]) == HypothesisStatus.SUPPORTED


def test_hypothesis_status_contradicted():
    assert derive_hypothesis_status([_ev(stance="contradict")]) == HypothesisStatus.CONTRADICTED


def test_hypothesis_status_conflicted_when_both_stances_present():
    rows = [_ev(evidence_id="E1", stance="support"), _ev(evidence_id="E2", stance="contradict")]
    assert derive_hypothesis_status(rows) == HypothesisStatus.CONFLICTED


def test_hypothesis_status_insufficient_when_evidence_but_no_stance():
    rows = [_ev(stance=None)]
    assert derive_hypothesis_status(rows) == HypothesisStatus.INSUFFICIENT


def test_hypothesis_status_insufficient_when_only_error_rows():
    rows = [_ev(status="ERROR", stance=None)]
    assert derive_hypothesis_status(rows) == HypothesisStatus.INSUFFICIENT


def test_hypothesis_status_ignores_stance_on_error_rows():
    # 1 dòng ERROR không nên đóng góp stance dù bị gán nhầm
    rows = [_ev(status="ERROR", stance="support")]
    assert derive_hypothesis_status(rows) == HypothesisStatus.INSUFFICIENT


def _view(hyp_id, status, evidence=None):
    return HypothesisView(id=hyp_id, statement="s", status=status, evidence=evidence or [])


def test_final_status_empty_hypotheses_is_insufficient_basis():
    assert derive_final_statuses([]) == (
        FinalStatus.INSUFFICIENT_BASIS.value,
        FinalStatus.INSUFFICIENT_BASIS.value,
    )


def test_final_status_any_conflicted_wins_over_everything():
    views = [
        _view("H1", HypothesisStatus.CONFLICTED),
        _view(
            "H2",
            HypothesisStatus.SUPPORTED,
            [_ev(tool_name="read_extended_traffic_window", stance="support")],
        ),
    ]
    final, original = derive_final_statuses(views)
    assert final == FinalStatus.CONFLICTING_EVIDENCE.value
    assert original == FinalStatus.CONFLICTING_EVIDENCE.value


def test_final_status_all_insufficient():
    views = [_view("H1", HypothesisStatus.INSUFFICIENT), _view("H2", HypothesisStatus.INSUFFICIENT)]
    assert derive_final_statuses(views) == (
        FinalStatus.INSUFFICIENT_BASIS.value,
        FinalStatus.INSUFFICIENT_BASIS.value,
    )


def test_final_status_confident_requires_structured_evidence():
    views = [
        _view(
            "H1",
            HypothesisStatus.SUPPORTED,
            [_ev(tool_name="read_extended_traffic_window", stance="support")],
        )
    ]
    final, original = derive_final_statuses(views)
    assert final == FinalStatus.CONFIDENT.value
    assert original == FinalStatus.CONFIDENT.value


def test_final_status_downgrades_to_needs_monitoring_without_structured_evidence():
    views = [
        _view(
            "H1",
            HypothesisStatus.SUPPORTED,
            [_ev(tool_name="read_pr_descriptions", stance="support")],
        )
    ]
    final, original = derive_final_statuses(views)
    assert final == FinalStatus.NEEDS_MONITORING.value
    # original_agent_status ghi lại trạng thái TRƯỚC khi bị corroboration hạ cấp (mục 11.3)
    assert original == FinalStatus.CONFIDENT.value


def test_final_status_mixed_without_supported_or_conflicted_is_insufficient_basis():
    views = [_view("H1", HypothesisStatus.PROPOSED), _view("H2", HypothesisStatus.CONTRADICTED)]
    final, original = derive_final_statuses(views)
    assert final == FinalStatus.INSUFFICIENT_BASIS.value


def test_distinct_evidence_sources_dedupes_same_pr_number():
    rows = [
        _ev(evidence_id="E1", tool_name="read_pr_descriptions", pr_number="42", status="OK"),
        _ev(evidence_id="E2", tool_name="read_source_code", pr_number="42", status="OK"),
    ]
    assert distinct_evidence_sources(rows) == 1


def test_distinct_evidence_sources_counts_different_prs_and_tools_separately():
    rows = [
        _ev(evidence_id="E1", tool_name="read_pr_descriptions", pr_number="42", status="OK"),
        _ev(evidence_id="E2", tool_name="read_source_code", pr_number="7", status="OK"),
        _ev(evidence_id="E3", tool_name="read_internal_docs", pr_number=None, status="OK"),
    ]
    assert distinct_evidence_sources(rows) == 3


def test_distinct_evidence_sources_ignores_error_rows():
    rows = [
        _ev(evidence_id="E1", status="OK"),
        _ev(evidence_id="E2", status="ERROR"),
    ]
    assert distinct_evidence_sources(rows) == 1
