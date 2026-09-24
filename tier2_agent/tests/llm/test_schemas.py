import jsonschema
import pytest

from tier2_agent.llm.schemas import build_model_output_schema

TOOL_NAMES = [
    "read_source_code",
    "read_pr_descriptions",
    "read_internal_docs",
    "read_extended_traffic_window",
]


def test_new_hypotheses_present_when_cap_not_reached():
    schema = build_model_output_schema(TOOL_NAMES, hypothesis_cap_reached=False)
    assert "new_hypotheses" in schema["properties"]


def test_new_hypotheses_absent_when_cap_reached():
    schema = build_model_output_schema(TOOL_NAMES, hypothesis_cap_reached=True)
    assert "new_hypotheses" not in schema["properties"]


def test_schema_rejects_new_hypotheses_when_cap_reached_due_to_additional_properties():
    schema = build_model_output_schema(TOOL_NAMES, hypothesis_cap_reached=True)
    payload = {
        "reasoning": "r",
        "action": "call_tool",
        "new_hypotheses": [{"id": "H9", "statement": "s"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_schema_accepts_minimal_valid_payload():
    schema = build_model_output_schema(TOOL_NAMES, hypothesis_cap_reached=False)
    payload = {"reasoning": "r", "action": "conclude"}
    jsonschema.validate(payload, schema)  # không raise


def test_schema_tool_name_enum_rejects_unknown_tool():
    schema = build_model_output_schema(TOOL_NAMES, hypothesis_cap_reached=False)
    payload = {
        "reasoning": "r",
        "action": "call_tool",
        "tool_calls": [{"tool_name": "delete_everything", "tool_input": {}, "hypothesis_id": "H1"}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_schema_action_enum_rejects_invalid_action():
    schema = build_model_output_schema(TOOL_NAMES, hypothesis_cap_reached=False)
    payload = {"reasoning": "r", "action": "do_whatever"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_schema_requires_reasoning_and_action():
    schema = build_model_output_schema(TOOL_NAMES, hypothesis_cap_reached=False)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"action": "conclude"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"reasoning": "r"}, schema)
