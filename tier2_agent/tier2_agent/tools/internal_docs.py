"""`read_internal_docs(service, keyword)` (TIER2_DESIGN.md mục 2).

Đọc README (và các file tài liệu nội bộ khác nếu có) của service, lọc theo
keyword. Cùng cơ chế subprocess list-args như `read_source_code` (mục 2.1).
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, Optional

from ..exceptions import ToolExecutionError
from ..tool_registry import ToolResult
from ._shared import grep_list_args, is_stale, last_commit_epoch

_DOC_FILENAMES = ("README.md", "README.rst", "README", "docs")


def make_internal_docs_tool(
    service_dirs: Dict[str, str],
    *,
    stale_threshold_days: int,
    timeout: float = 30.0,
    reference_epoch: Optional[float] = None,
    commit_lookup: Callable[[str], Optional[float]] = last_commit_epoch,
) -> Callable[[dict], ToolResult]:
    def _impl(tool_input: dict) -> ToolResult:
        service = tool_input["service"]
        keyword = tool_input["keyword"]
        root = service_dirs.get(service)
        if root is None:
            raise ToolExecutionError(
                f"Không có source directory cấu hình cho service {service!r}"
            )
        if not os.path.isdir(root):
            raise ToolExecutionError(f"Thư mục source không tồn tại: {root!r}")

        doc_paths = [
            os.path.join(root, name)
            for name in _DOC_FILENAMES
            if os.path.exists(os.path.join(root, name))
        ]
        if not doc_paths:
            return ToolResult(
                raw_output=f"Service {service!r} không có README/tài liệu nội bộ nào."
            )

        matches = []
        for path in doc_paths:
            out = grep_list_args(keyword, path, timeout=timeout)
            if out:
                matches.append(out)

        if not matches:
            return ToolResult(
                raw_output=(
                    f"Không tìm thấy dòng nào khớp keyword={keyword!r} trong tài "
                    f"liệu nội bộ của {service!r}."
                )
            )

        stale = is_stale(
            root,
            stale_threshold_days,
            reference_epoch=reference_epoch if reference_epoch is not None else time.time(),
            commit_lookup=commit_lookup,
        )
        return ToolResult(raw_output="\n".join(matches), stale=stale)

    return _impl
