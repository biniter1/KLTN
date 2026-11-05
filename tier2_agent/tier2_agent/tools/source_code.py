"""`read_source_code(source_service, keyword)` (TIER2_DESIGN.md mục 2).

Quét code thật + comment + Dockerfile trong thư mục source của service.
`service_dirs` ánh xạ canonical_service -> đường dẫn thư mục source thật
trên đĩa (do người vận hành PoC cấu hình, ví dụ checkout của Online
Boutique + service tự cấy).
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, Dict, Optional, Tuple

from ..exceptions import ToolExecutionError
from ..tool_registry import ToolResult
from ._shared import extract_pr_numbers, grep_list_args, is_stale, last_commit_epoch


# F3 (05/11): file SINH TỰ ĐỘNG từ .proto (và chính file .proto) chỉ chứa định nghĩa
# stub/client/message cho MỌI service trong hệ thống -- không phải bằng chứng service
# nguồn thật sự gọi đích. Benchmark 04/11: 8 phiên "support" chỉ dựa vào các file này, và
# chúng còn chiếm hết trần 4000 ký tự output khiến code thật bị cắt mất.
GENERATED_FILE_RE = re.compile(
    r"(^|/)(genproto|generated)/|\.pb\.go$|_pb2(_grpc)?\.pyi?$|\.proto$|\.pb\.(cc|h)$|_grpc_pb\.js$|_pb\.js$"
)


def filter_generated(grep_output: str) -> Tuple[str, int, int]:
    """Bỏ các dòng grep (`path:line:text`) thuộc file sinh tự động.
    Trả (output còn lại, số dòng bị bỏ, số file bị bỏ)."""
    kept, dropped_files, dropped = [], set(), 0
    for line in grep_output.splitlines():
        path = line.split(":", 1)[0]
        if GENERATED_FILE_RE.search(path.replace("\\", "/")):
            dropped += 1
            dropped_files.add(path)
        else:
            kept.append(line)
    return "\n".join(kept), dropped, len(dropped_files)


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
        output, dropped_lines, dropped_files = filter_generated(output)
        note = (f"\n[đã bỏ {dropped_lines} dòng khớp trong {dropped_files} file sinh tự động từ .proto "
                f"— không phải bằng chứng gọi]" if dropped_lines else "")
        if not output:
            return ToolResult(
                raw_output=(
                    f"Không tìm thấy dòng nào khớp keyword={keyword!r} trong "
                    f"source code của {service!r} (bao gồm Dockerfile), ngoài file sinh tự động."
                    + note
                ),
                no_match=True,
            )
        output = output + note

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
