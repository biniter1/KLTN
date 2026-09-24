"""Tool Registry (TIER2_DESIGN.md mục 2, 2.1, 3).

Module tĩnh giữ định nghĩa 4 tool (`{name, description, input_schema,
scope}`) — nguồn duy nhất cho `available_tools` trong Context Manager.build.

Đồng thời chịu trách nhiệm kiểm soát INPUT (mục 2.1) — chạy TRƯỚC khi tool
thật thực thi:
  - Whitelist: tên tool (enum 4 giá trị), `source_service`/`service` phải
    khớp canonical_service của đúng cạnh đang xác minh, tool scope read-only.
  - Policy: độ dài/charset của `keyword`.

Việc thực thi thật (chống command injection ở tầng OS bằng subprocess
list-args) nằm ở từng implementation trong `tier2_agent/tools/`; Tool
Registry chỉ định tuyến + validate, không tự đọc file/gọi mạng.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Optional

from .config import Tier2Config
from .exceptions import ToolExecutionError, ToolInputRejected, UnknownToolError
from .models import ToolName


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    scope: str = "read-only"


@dataclass
class ToolResult:
    raw_output: str
    stale: bool = False
    pr_number: Optional[str] = None


_DEFINITIONS: Dict[str, ToolDefinition] = {
    ToolName.READ_SOURCE_CODE.value: ToolDefinition(
        name=ToolName.READ_SOURCE_CODE.value,
        description=(
            "Quét code thật + comment + Dockerfile trong thư mục source của "
            "service, lọc theo keyword."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source_service": {"type": "string"},
                "keyword": {"type": "string"},
            },
            "required": ["source_service", "keyword"],
            "additionalProperties": False,
        },
    ),
    ToolName.READ_PR_DESCRIPTIONS.value: ToolDefinition(
        name=ToolName.READ_PR_DESCRIPTIONS.value,
        description="Lấy mô tả PR qua GitHub API, qua gatekeeper API Server.",
        input_schema={
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
            "additionalProperties": False,
        },
    ),
    ToolName.READ_INTERNAL_DOCS.value: ToolDefinition(
        name=ToolName.READ_INTERNAL_DOCS.value,
        description="Đọc README của service, lọc theo keyword.",
        input_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "keyword": {"type": "string"},
            },
            "required": ["service", "keyword"],
            "additionalProperties": False,
        },
    ),
    ToolName.READ_EXTENDED_TRAFFIC_WINDOW.value: ToolDefinition(
        name=ToolName.READ_EXTENDED_TRAFFIC_WINDOW.value,
        description="Mở rộng cửa sổ quan sát traffic cho đúng cạnh đang xác minh.",
        input_schema={
            "type": "object",
            "properties": {
                "edge": {"type": "string"},
                "window": {"type": "integer", "minimum": 1},
            },
            "required": ["edge", "window"],
            "additionalProperties": False,
        },
    ),
}

_MAX_WINDOW_DAYS = 90


class ToolRegistry:
    """Định tuyến + validate input, KHÔNG tự thực thi tool.

    `implementations` là map tool_name -> callable(tool_input: dict) ->
    ToolResult, tiêm từ ngoài vào để dễ test/đổi provider (Prometheus thật,
    gatekeeper thật, ...).
    """

    def __init__(self, config: Tier2Config, implementations: Dict[str, Callable]):
        self._config = config
        self._impls = implementations

    @staticmethod
    def available_tools() -> list:
        return list(_DEFINITIONS.values())

    @staticmethod
    def definition(tool_name: str) -> ToolDefinition:
        try:
            return _DEFINITIONS[tool_name]
        except KeyError as exc:
            raise UnknownToolError(f"Tool không tồn tại: {tool_name!r}") from exc

    def _validate_keyword(self, keyword: str) -> None:
        if not isinstance(keyword, str) or not keyword:
            raise ToolInputRejected("keyword rỗng hoặc không phải chuỗi")
        if len(keyword) > self._config.keyword_max_length:
            raise ToolInputRejected(
                f"keyword vượt quá độ dài cho phép ({self._config.keyword_max_length})"
            )
        allowed = set(self._config.keyword_allowed_charset)
        bad_chars = set(keyword) - allowed
        if bad_chars:
            raise ToolInputRejected(
                f"keyword chứa ký tự không được phép: {sorted(bad_chars)!r}"
            )

    def validate_input(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        allowed_services: FrozenSet[str],
        edge_identity_key: str,
    ) -> None:
        """Raise `ToolInputRejected`/`UnknownToolError` nếu input không hợp lệ.

        Không đoán/sanitize thay — khớp thì cho qua, không khớp thì từ chối
        thẳng (mục 2.1).
        """

        definition = self.definition(tool_name)  # raise UnknownToolError nếu sai tên
        if definition.scope != "read-only":
            raise ToolInputRejected(f"Tool {tool_name!r} không phải read-only")

        if not isinstance(tool_input, dict):
            raise ToolInputRejected("tool_input phải là object")

        if tool_name in (
            ToolName.READ_SOURCE_CODE.value,
            ToolName.READ_PR_DESCRIPTIONS.value,
            ToolName.READ_INTERNAL_DOCS.value,
        ):
            service_key = "source_service" if tool_name == ToolName.READ_SOURCE_CODE.value else "service"
            service = tool_input.get(service_key)
            if service not in allowed_services:
                raise ToolInputRejected(
                    f"{service_key}={service!r} không khớp canonical_service nào của cạnh đang xác minh"
                )
            if "keyword" in definition.input_schema["required"]:
                self._validate_keyword(tool_input.get("keyword"))

        elif tool_name == ToolName.READ_EXTENDED_TRAFFIC_WINDOW.value:
            edge = tool_input.get("edge")
            if edge != edge_identity_key:
                raise ToolInputRejected(
                    "edge không khớp identity_key của cạnh đang xác minh trong phiên này"
                )
            window = tool_input.get("window")
            if not isinstance(window, int) or isinstance(window, bool) or not (1 <= window <= _MAX_WINDOW_DAYS):
                raise ToolInputRejected(
                    f"window phải là số nguyên trong khoảng [1, {_MAX_WINDOW_DAYS}]"
                )
        else:  # pragma: no cover - defensive, _DEFINITIONS đã cover hết ToolName
            raise UnknownToolError(tool_name)

    def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
        impl = self._impls.get(tool_name)
        if impl is None:
            raise UnknownToolError(f"Không có implementation cho tool {tool_name!r}")
        try:
            return impl(tool_input)
        except ToolExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - bọc lại thành lỗi tool thống nhất
            raise ToolExecutionError(f"{tool_name} lỗi: {exc}") from exc
