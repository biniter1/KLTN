"""`read_source_code(source_service, keyword)` (TIER2_DESIGN.md mục 2).

Quét code thật + comment + Dockerfile trong thư mục source của service.
`service_dirs` ánh xạ canonical_service -> đường dẫn thư mục source thật
trên đĩa (do người vận hành PoC cấu hình, ví dụ checkout của Online
Boutique + service tự cấy).
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, Optional

from ..exceptions import ToolExecutionError
from ..tool_registry import ToolResult
from ._shared import extract_pr_numbers, grep_list_args, is_stale, last_commit_epoch


def make_source_code_tool(
    service_dirs: Dict[str, str],
    *,
    stale_threshold_days: int,
    timeout: float = 30.0,
    reference_epoch: Optional[float] = None,
    commit_lookup: Callable[[str], Optional[float]] = last_commit_epoch,
) -> Callable[[dict], ToolResult]:
    def _impl(tool_input: dict) -> ToolResult:
        service = tool_input["source_service"]
        keyword = tool_input["keyword"]
        root = service_dirs.get(service)
        if root is None:
            raise ToolExecutionError(
                f"Không có source directory cấu hình cho service {service!r}"
            )
        if not os.path.isdir(root):
            raise ToolExecutionError(f"Thư mục source không tồn tại: {root!r}")

        output = grep_list_args(keyword, root, timeout=timeout)
        if not output:
            return ToolResult(
                raw_output=(
                    f"Không tìm thấy dòng nào khớp keyword={keyword!r} trong "
                    f"source code của {service!r} (bao gồm Dockerfile)."
                )
            )

        stale = is_stale(
            root,
            stale_threshold_days,
            reference_epoch=reference_epoch if reference_epoch is not None else time.time(),
            commit_lookup=commit_lookup,
        )
        return ToolResult(
            raw_output=output,
            stale=stale,
            pr_number=extract_pr_numbers(output),
        )

    return _impl
