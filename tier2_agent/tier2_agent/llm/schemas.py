"""JSON schema của "Model Output" (TIER2_DESIGN.md mục 6).

Ép bằng structured output/JSON schema của API LLM — không hy vọng Model tự
giác tuân thủ định dạng. Field `new_hypotheses` bị loại HẲN khỏi schema khi
đã chạm trần hypothesis mở song song (mục 7.6: "loại bỏ hẳn lựa chọn 'mở
hypothesis mới' khỏi schema hành động hợp lệ của vòng đó, Model không có
cách nào chọn được, dù muốn") — đây là lý do schema phải được build ĐỘNG
mỗi vòng thay vì 1 hằng số tĩnh.
"""

from __future__ import annotations

from typing import List


_STANCE_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "evidence_id": {"type": "string"},
        "stance": {"type": "string", "enum": ["support", "contradict"]},
    },
    "required": ["evidence_id", "stance"],
    "additionalProperties": False,
}

_NEW_HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "statement": {"type": "string"},
    },
    "required": ["id", "statement"],
    "additionalProperties": False,
}


def _tool_call_schema(tool_names: List[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "enum": list(tool_names)},
            "tool_input": {"type": "object"},
            "hypothesis_id": {"type": "string"},
        },
        "required": ["tool_name", "tool_input", "hypothesis_id"],
        "additionalProperties": False,
    }


def build_model_output_schema(
    tool_names: List[str], *, hypothesis_cap_reached: bool
) -> dict:
    properties = {
        "reasoning": {"type": "string"},
        "stance_updates": {"type": "array", "items": _STANCE_UPDATE_SCHEMA},
        "action": {"type": "string", "enum": ["call_tool", "conclude"]},
        "tool_calls": {
            "type": "array",
            "items": _tool_call_schema(tool_names),
        },
    }
    required = ["reasoning", "action"]

    if not hypothesis_cap_reached:
        properties["new_hypotheses"] = {
            "type": "array",
            "items": _NEW_HYPOTHESIS_SCHEMA,
        }

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
