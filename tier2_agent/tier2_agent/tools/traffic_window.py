"""`read_extended_traffic_window(edge, window)` (TIER2_DESIGN.md mục 2).

Duy nhất trong 4 tool trả về bằng chứng "cấu trúc" (số liệu traffic cùng
kiểu Tier 1) — dùng cho corroboration rule (mục 7.4). `TrafficProvider` là
interface tối thiểu; cài đặt thật (Prometheus/Envoy qua
`TRAFFIC_COLLECTION_PIPELINE.md`, hoặc qua gatekeeper của API Server cùng
kiểu `read_pr_descriptions`) nằm ngoài phạm vi gói này — xem README phần
"Điểm mở/diễn giải": thiết kế không nói rõ tool này có bắt buộc đi qua
gatekeeper hay không (khác `read_pr_descriptions` được nêu tường minh), nên
để interface pluggable thay vì đoán.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from ..exceptions import ToolExecutionError
from ..tool_registry import ToolResult


class TrafficProvider(Protocol):
    def fetch_extended_window(self, edge: str, window_days: int) -> dict:
        """Trả dict tối thiểu {"count": int, "distinct_days": int,
        "error_ratio": float, "protocol_diversity": int}."""
        ...


def make_traffic_window_tool(
    provider: TrafficProvider,
) -> Callable[[dict], ToolResult]:
    def _impl(tool_input: dict) -> ToolResult:
        edge = tool_input["edge"]
        window = tool_input["window"]
        try:
            stats = provider.fetch_extended_window(edge, window)
        except ToolExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolExecutionError(f"read_extended_traffic_window lỗi: {exc}") from exc

        count = stats.get("count", 0)
        distinct_days = stats.get("distinct_days", 0)
        error_ratio = stats.get("error_ratio", 0.0)
        raw_output = (
            f"Cửa sổ mở rộng {window} ngày cho edge={edge!r}: "
            f"count={count}, distinct_days={distinct_days}, error_ratio={error_ratio:.3f}"
        )
        return ToolResult(raw_output=raw_output)

    return _impl
