"""Helper dùng chung giữa `read_source_code` và `read_internal_docs`.

Điểm mấu chốt theo TIER2_DESIGN.md mục 2.1: bắt buộc `subprocess.run` với
danh sách argument, KHÔNG BAO GIỜ ghép chuỗi rồi `shell=True` — chặn
path-traversal/command-injection ở tầng hệ điều hành, không phụ thuộc việc
lọc `keyword` ở Tool Registry có bắt hết ký tự nguy hiểm hay không (phòng
thủ theo lớp — Tool Registry đã lọc, đây là lớp thứ 2 độc lập).
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

from ..exceptions import ToolExecutionError

_PR_NUMBER_RE = re.compile(r"(?:PR[-\s#]?|#)(\d{1,6})\b")


def grep_list_args(keyword: str, root: str, *, timeout: float) -> str:
    """Chạy grep đệ quy trên `root`, tìm `keyword`, trả stdout (rỗng nếu
    không khớp gì). KHÔNG dùng shell=True, mọi argument truyền dạng list."""

    try:
        proc = subprocess.run(
            ["grep", "-rn", "-e", keyword, root],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(f"timeout khi quét {root!r}") from exc
    except FileNotFoundError as exc:
        raise ToolExecutionError("thiếu công cụ grep trong môi trường thực thi") from exc

    # grep trả 0 = có khớp, 1 = không khớp (không phải lỗi), >=2 = lỗi thật
    if proc.returncode not in (0, 1):
        raise ToolExecutionError(f"grep lỗi (exit={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def last_commit_epoch(path: str, *, timeout: float = 10.0) -> Optional[float]:
    """Ngày commit cuối (epoch giây) qua `git log`, dùng cho staleness (mục 7.5).

    Trả None nếu không xác định được (không phải git repo, git không có sẵn,
    ...) — cố tình KHÔNG suy ra stale=true khi không biết chắc, tránh
    false positive.
    """

    try:
        proc = subprocess.run(
            ["git", "-C", path, "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None


def is_stale(
    path: str,
    threshold_days: int,
    *,
    reference_epoch: Optional[float] = None,
    commit_lookup=last_commit_epoch,
) -> bool:
    commit_epoch = commit_lookup(path)
    if commit_epoch is None:
        return False
    ref = reference_epoch if reference_epoch is not None else time.time()
    age_days = (ref - commit_epoch) / 86400.0
    return age_days > threshold_days


def extract_pr_numbers(text: str) -> Optional[str]:
    """Tìm số PR nhắc tới trong text (comment code, PR description) — dùng
    cho luật độc lập nguồn (mục 7.5: PR description + source code cùng trỏ
    1 PR number tính là 1 nguồn, không phải 2)."""

    numbers = sorted(set(_PR_NUMBER_RE.findall(text or "")), key=int)
    if not numbers:
        return None
    return ",".join(numbers)
