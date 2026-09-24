"""Agent Runtime — lớp mỏng duy nhất việc gọi Model (TIER2_DESIGN.md mục 1).

Tách riêng khỏi `Brain` để dễ test/đổi nhà cung cấp LLM. Luôn validate raw
JSON trả về đúng JSON schema trước khi convert sang dataclass — "không hy
vọng Model tự giác tuân thủ định dạng" dù đã ép structured output ở lớp
`ModelClient` (mục 6).
"""

from __future__ import annotations

from typing import List

import jsonschema

from .context_manager import ModelContext
from .exceptions import Tier2Error
from .llm.schemas import build_model_output_schema
from .models import ModelAction, ModelOutput, NewHypothesis, StanceUpdate, ToolCallRequest


class ModelOutputSchemaError(Tier2Error):
    """Model trả JSON không đúng schema Model Output (mục 6) dù đã ép structured output."""


class AgentRuntime:
    def __init__(self, model_client):
        self._client = model_client

    def call(self, context: ModelContext, tool_names: List[str]) -> ModelOutput:
        schema = build_model_output_schema(
            tool_names, hypothesis_cap_reached=context.hypothesis_cap_reached
        )
        raw = self._client.generate(context, tool_names)
        try:
            jsonschema.validate(raw, schema)
        except jsonschema.ValidationError as exc:
            raise ModelOutputSchemaError(f"Model Output sai schema: {exc.message}") from exc

        return ModelOutput(
            reasoning=raw["reasoning"],
            action=raw["action"],
            stance_updates=[
                StanceUpdate(evidence_id=su["evidence_id"], stance=su["stance"])
                for su in raw.get("stance_updates", [])
            ],
            new_hypotheses=[
                NewHypothesis(id=nh["id"], statement=nh["statement"])
                for nh in raw.get("new_hypotheses", [])
            ],
            tool_calls=[
                ToolCallRequest(
                    tool_name=tc["tool_name"],
                    tool_input=tc["tool_input"],
                    hypothesis_id=tc["hypothesis_id"],
                )
                for tc in raw.get("tool_calls", [])
            ],
        )
