from tier2_agent.config import Tier2Config
from tier2_agent.context_manager import build, format_context, normalize
from tier2_agent.escalation import EscalationRecord
from tier2_agent.models import EvidenceRow, Hypothesis, HypothesisStatus
from tier2_agent.tool_registry import ToolRegistry


def _evidence(**kwargs):
    base = dict(
        evidence_id="E1",
        run_id="run1",
        iteration=1,
        hypothesis_id="H1",
        tool_name="read_source_code",
        tool_input={"source_service": "frontend", "keyword": "x"},
        tool_output="found something",
        stance=None,
        stale=False,
        status="OK",
    )
    base.update(kwargs)
    return EvidenceRow(**base)


def test_build_groups_evidence_by_hypothesis_and_computes_status():
    hyps = {"H1": Hypothesis(id="H1", statement="H1 statement")}
    rows = [_evidence(evidence_id="E1", stance="support")]
    views = build(rows, hyps)
    assert set(views) == {"H1"}
    assert views["H1"].status == HypothesisStatus.SUPPORTED
    assert views["H1"].statement == "H1 statement"
    assert len(views["H1"].evidence) == 1


def test_build_orders_evidence_by_reliability_then_iteration():
    hyps = {"H1": Hypothesis(id="H1", statement="s")}
    rows = [
        _evidence(evidence_id="E1", tool_name="read_internal_docs", iteration=1),
        _evidence(evidence_id="E2", tool_name="read_extended_traffic_window", iteration=2),
        _evidence(evidence_id="E3", tool_name="read_pr_descriptions", iteration=1),
    ]
    views = build(rows, hyps)
    ordered_ids = [e.evidence_id for e in views["H1"].evidence]
    # traffic (rank 0) > PR (rank 1) > docs (rank 3), bất kể iteration
    assert ordered_ids == ["E2", "E3", "E1"]


def test_build_demotes_stale_evidence_in_ordering():
    hyps = {"H1": Hypothesis(id="H1", statement="s")}
    rows = [
        _evidence(evidence_id="FRESH", tool_name="read_internal_docs", stale=False, iteration=1),
        _evidence(evidence_id="STALE_PR", tool_name="read_pr_descriptions", stale=True, iteration=1),
    ]
    views = build(rows, hyps)
    ordered_ids = [e.evidence_id for e in views["H1"].evidence]
    # PR bị stale nên bị tụt hạng, đứng sau docs không stale dù docs có rank cơ bản kém hơn
    assert ordered_ids == ["FRESH", "STALE_PR"]


def test_build_handles_evidence_for_unknown_hypothesis_gracefully():
    rows = [_evidence(hypothesis_id="H_ORPHAN")]
    views = build(rows, {})
    assert "H_ORPHAN" in views
    assert "không rõ nguồn gốc" in views["H_ORPHAN"].statement


def test_normalize_noop_when_under_budget():
    cfg = Tier2Config(context_window_tokens=1_000_000, normalize_token_budget_ratio=0.7)
    hyps = {"H1": Hypothesis(id="H1", statement="s")}
    rows = [_evidence(evidence_id="E1", tool_output="short")]
    views = build(rows, hyps)
    result = normalize(views, cfg)
    assert len(result["H1"].evidence) == 1


def test_normalize_trims_oldest_round_first_when_over_budget():
    cfg = Tier2Config(context_window_tokens=40, normalize_token_budget_ratio=1.0, chars_per_token_estimate=1.0)
    hyps = {"H1": Hypothesis(id="H1", statement="s")}
    rows = [
        _evidence(evidence_id="OLD", iteration=1, tool_output="x" * 30, tool_name="read_internal_docs"),
        _evidence(evidence_id="NEW", iteration=2, tool_output="y" * 30, tool_name="read_internal_docs"),
    ]
    views = build(rows, hyps)
    result = normalize(views, cfg)
    remaining_ids = {e.evidence_id for e in result["H1"].evidence}
    assert "OLD" not in remaining_ids
    assert "NEW" in remaining_ids


def test_normalize_does_not_mutate_input_views():
    cfg = Tier2Config(context_window_tokens=10, normalize_token_budget_ratio=1.0, chars_per_token_estimate=1.0)
    hyps = {"H1": Hypothesis(id="H1", statement="s")}
    rows = [_evidence(evidence_id="E1", iteration=1, tool_output="x" * 50)]
    views = build(rows, hyps)
    original_len = len(views["H1"].evidence)
    normalize(views, cfg)
    assert len(views["H1"].evidence) == original_len


def test_format_context_wraps_evidence_in_tags_and_includes_system_instruction(escalation_dict_factory):
    escalation = EscalationRecord.from_dict(escalation_dict_factory())
    hyps = {"H1": Hypothesis(id="H1", statement="s")}
    rows = [_evidence(evidence_id="E1", tool_output="raw finding")]
    views = build(rows, hyps)
    ctx = format_context(
        views,
        escalation=escalation,
        available_tools=ToolRegistry.available_tools(),
        escalation_reason="test reason",
        hypothesis_cap_reached=False,
    )
    ev = ctx.hypotheses[0]["evidence"][0]
    assert ev["tool_output"] == '<evidence source="read_source_code">raw finding</evidence>'
    assert "không phải" in ctx.system_instruction.lower() or "khong phai" in ctx.system_instruction.lower()
    assert ctx.goal["escalation_reason"] == "test reason"
    assert ctx.goal["dependency_status"] == escalation.dependency_status
    assert ctx.hypothesis_cap_reached is False
    assert len(ctx.available_tools) == 4


def test_format_context_carries_system_notices_through():
    ctx = format_context(
        {},
        escalation=EscalationRecord.from_dict(
            {
                "source_namespace": "default",
                "source_canonical_service": "a",
                "source_service_account": "a-sa",
                "destination_namespace": "default",
                "destination_canonical_service": "b",
                "protocol": "TCP",
                "port": 80,
                "dependency_status": "UNKNOWN",
                "config_finding": None,
            }
        ),
        available_tools=ToolRegistry.available_tools(),
        escalation_reason="r",
        hypothesis_cap_reached=True,
        system_notices=["evidence_id E99 does not exist"],
    )
    assert ctx.system_notices == ["evidence_id E99 does not exist"]
    assert ctx.hypothesis_cap_reached is True
