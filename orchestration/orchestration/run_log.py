"""`pipeline_run_log` — ghi trạng thái từng bước của 1 attempt (ORCHESTRATION_DESIGN.md mục 2,
DASHBOARD_DESIGN.md mục 5 điểm 1). CHỈ Batch Orchestrator dùng module này.

Mỗi bước sinh 2 dòng: RUNNING lúc bắt đầu, SUCCESS/FAILED lúc kết thúc — append, không UPDATE
dòng RUNNING, để lịch sử "bắt đầu lúc nào, kết thúc lúc nào" còn nguyên cho dashboard.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StepFailed(RuntimeError):
    """1 bước lỗi -> cả trial bị hủy (mục 2). Mang theo tên bước để ghi dòng `trial` FAILED."""

    def __init__(self, step: str, message: str):
        super().__init__(f"[{step}] {message}")
        self.step = step
        self.message = message


class StepContext:
    """Bước đang chạy có thể đính kèm `detail` (JSON) vào dòng SUCCESS của mình."""

    def __init__(self) -> None:
        self.detail: dict = {}


class PipelineRunLog:
    def __init__(self, conn: sqlite3.Connection, run_id: str, attempt_id: str,
                 clock: Callable[[], str] = utc_now_iso):
        self._conn = conn
        self.run_id = run_id
        self.attempt_id = attempt_id
        self._clock = clock

    def record(self, step: str, status: str, *, error_message: Optional[str] = None,
               detail: Optional[dict] = None) -> None:
        self._conn.execute(
            "INSERT INTO pipeline_run_log (run_id, attempt_id, step, status, error_message, detail, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, self.attempt_id, step, status, error_message,
             json.dumps(detail, ensure_ascii=False, default=str) if detail else None, self._clock()),
        )

    @contextmanager
    def step(self, step: str) -> Iterator[StepContext]:
        """Ghi RUNNING -> chạy thân -> SUCCESS (kèm detail) hoặc FAILED (kèm lỗi) rồi
        ném `StepFailed`. KHÔNG nuốt lỗi: không có đường nào để bước sau chạy tiếp âm thầm."""
        self.record(step, "RUNNING")
        ctx = StepContext()
        try:
            yield ctx
        except StepFailed as exc:
            self.record(step, "FAILED", error_message=exc.message, detail=ctx.detail or None)
            raise
        except Exception as exc:  # noqa: BLE001 — mọi lỗi đều phải thành FAILED có ghi lại
            message = f"{type(exc).__name__}: {exc}"
            detail = dict(ctx.detail)
            detail["traceback"] = traceback.format_exc(limit=8)
            self.record(step, "FAILED", error_message=message, detail=detail)
            raise StepFailed(step, message) from exc
        self.record(step, "SUCCESS", detail=ctx.detail or None)


def next_attempt_id(conn: sqlite3.Connection, run_id: str) -> str:
    """`<run_id>.a<n>` — n = số attempt đã có của trial + 1. Chạy lại 1 trial (sau FAILED
    hoặc chủ động chạy lại) luôn ra attempt MỚI, không ghi đè dữ liệu attempt cũ."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT attempt_id) FROM pipeline_run_log WHERE run_id = ?", (run_id,)
    ).fetchone()
    return f"{run_id}.a{int(row[0]) + 1}"
