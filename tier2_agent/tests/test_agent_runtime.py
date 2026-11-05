import pytest

from tier2_agent.agent_runtime import AgentRuntime, ModelOutputSchemaError
from tier2_agent.context_manager import ModelContext
from tier2_agent.llm.client import ScriptedModelClient
from tier2_agent.models import ModelOutput

TOOL_NAMES = ["read_source_code", "read_pr_descriptions", "read_internal_docs", "read_extended_traffic_window"]


def _ctx(hypothesis_cap_reached=False):
    return ModelContext(
        goal={},
        available_tools=[],
        hypotheses=[],
        system_instruction="x",
        system_notices=[],
        hypothesis_cap_reached=hypothesis_cap_reached,
    )


def test_valid_output_converted_to_dataclasses():
    raw = {
        "reasoning": "r",
        "action": "call_tool",
        "stance_updates": [{"evidence_id": "E1", "stance": "support"}],
        "new_hypotheses": [{"id": "H1", "statement": "s", "polarity": "pro"}],
        "tool_calls": [
            {"tool_name": "read_source_code", "tool_input": {"source_service": "frontend", "keyword": "x"}, "hypothesis_id": "H1"}
        ],
    }
    runtime = AgentRuntime(ScriptedModelClient(script=[raw]))
    result = runtime.call(_ctx(), TOOL_NAMES)
    assert isinstance(result, ModelOutput)
    assert result.reasoning == "r"
    assert result.stance_updates[0].evidence_id == "E1"
    assert result.new_hypotheses[0].id == "H1"
    assert result.tool_calls[0].tool_name == "read_source_code"


def test_missing_required_field_raises_schema_error():
    raw = {"action": "conclude"}  # thiếu reasoning
    runtime = AgentRuntime(ScriptedModelClient(script=[raw, raw]))  # sai cả 2 lần (có 1 lần gọi lại)
    with pytest.raises(ModelOutputSchemaError):
        runtime.call(_ctx(), TOOL_NAMES)


def test_new_hypotheses_rejected_when_cap_reached():
    raw = {"reasoning": "r", "action": "call_tool", "new_hypotheses": [{"id": "H9", "statement": "s", "polarity": "pro"}]}
    runtime = AgentRuntime(ScriptedModelClient(script=[raw, raw]))
    with pytest.raises(ModelOutputSchemaError):
        runtime.call(_ctx(hypothesis_cap_reached=True), TOOL_NAMES)


def test_defaults_are_empty_lists_when_omitted():
    raw = {"reasoning": "r", "action": "conclude"}
    runtime = AgentRuntime(ScriptedModelClient(script=[raw]))
    result = runtime.call(_ctx(), TOOL_NAMES)
    assert result.stance_updates == []
    assert result.new_hypotheses == []
    assert result.tool_calls == []


def test_unknown_tool_name_in_tool_calls_rejected():
    raw = {
        "reasoning": "r",
        "action": "call_tool",
        "tool_calls": [{"tool_name": "hack_the_mainframe", "tool_input": {}, "hypothesis_id": "H1"}],
    }
    runtime = AgentRuntime(ScriptedModelClient(script=[raw, raw]))
    with pytest.raises(ModelOutputSchemaError):
        runtime.call(_ctx(), TOOL_NAMES)


def test_schema_error_retried_once_with_notice_then_succeeds():
    """F5 (05/11): stance ngoài enum ('neutral') không còn làm dừng cả phiên."""
    bad = {"reasoning": "r", "action": "conclude",
           "stance_updates": [{"evidence_id": "E1", "stance": "neutral"}]}
    good = {"reasoning": "r2", "action": "conclude"}
    seen = []
    client = ScriptedModelClient(script_fn=lambda ctx, t: seen.append(list(ctx.system_notices)) or (bad if len(seen) == 1 else good))
    result = AgentRuntime(client).call(_ctx(), TOOL_NAMES)
    assert result.reasoning == "r2" and client.call_count == 2
    assert seen[0] == [] and "sai schema" in seen[1][0]


def test_new_hypothesis_requires_polarity():
    raw = {"reasoning": "r", "action": "call_tool", "new_hypotheses": [{"id": "H1", "statement": "s"}]}
    runtime = AgentRuntime(ScriptedModelClient(script=[raw, raw]))
    with pytest.raises(ModelOutputSchemaError):
        runtime.call(_ctx(), TOOL_NAMES)
