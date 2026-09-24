"""Điểm nối với `Batch Orchestrator` (TIER2_DESIGN.md mục 11.3).

"Brain trả Final Result về Batch Orchestrator (đồng bộ, giá trị trả về) ->
Batch Orchestrator UPDATE dependency_evidence, thêm 2 cột: tier2_status,
tier2_original_status." — `Brain.run_session()` (brain.py) đã làm đúng vế
đầu (trả `RoundResult` đồng bộ). Hàm ở đây làm vế sau, THAY MẶT Batch
Orchestrator, vì gói này không có quyền truy cập code Batch Orchestrator
thật (thuộc 1 phần khác của hệ thống, ORCHESTRATION_DESIGN.md).

NOTE (xem README "Điểm mở/diễn giải"): `dependency_evidence` là bảng CỦA
Tier 1/Batch Orchestrator, không phải bảng Tier 2 sở hữu
(`ORCHESTRATION_DESIGN.md` mục 3: "CHỈ Batch Orchestrator được ghi"). Gói
này không có DDL thật của bảng đó, nên hàm dưới đây:
  1. Không tự ý tạo bảng nếu nó chưa tồn tại — báo lỗi rõ ràng thay vì đoán
     schema, TRỪ khi gọi với `create_if_missing=True` (chỉ dành cho demo/
     test độc lập gói Tier 2, xem `tests/test_orchestrator_hook.py`).
  2. Chỉ ALTER TABLE thêm 2 cột mới nếu chưa có — không đụng cột nào khác.
  3. Định danh đúng 1 dòng để UPDATE bằng `run_id` + 7 field identity
     (TIER1_DESIGN.md mục 3: source_namespace, source_canonical_service,
     source_service_account, destination_namespace,
     destination_canonical_service, protocol, port) — không dùng 1 cột
     `edge_identity_key` gộp sẵn, vì schema mục 10 của Tier 1 lưu từng field
     rời, không lưu chuỗi gộp.
"""

from __future__ import annotations

import sqlite3

from .escalation import EscalationRecord
from .models import RoundResult

_IDENTITY_COLUMNS = (
    "source_namespace",
    "source_canonical_service",
    "source_service_account",
    "destination_namespace",
    "destination_canonical_service",
    "protocol",
    "port",
)

_MINIMAL_DDL = """
CREATE TABLE IF NOT EXISTS dependency_evidence (
    run_id TEXT NOT NULL,
    source_namespace TEXT,
    source_canonical_service TEXT,
    source_service_account TEXT,
    destination_namespace TEXT,
    destination_canonical_service TEXT,
    protocol TEXT,
    port INTEGER,
    dependency_status TEXT,
    tier2_status TEXT,
    tier2_original_status TEXT
);
"""


def _ensure_tier2_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dependency_evidence)")}
    if "tier2_status" not in cols:
        conn.execute("ALTER TABLE dependency_evidence ADD COLUMN tier2_status TEXT")
    if "tier2_original_status" not in cols:
        conn.execute("ALTER TABLE dependency_evidence ADD COLUMN tier2_original_status TEXT")


def apply_tier2_result(
    conn: sqlite3.Connection,
    escalation: EscalationRecord,
    run_id: str,
    result: RoundResult,
    *,
    create_if_missing: bool = False,
) -> int:
    """UPDATE `dependency_evidence` với 2 cột `tier2_status`/`tier2_original_status`
    (mục 11.3). Trả số dòng bị ảnh hưởng (0 nếu không tìm thấy dòng khớp —
    gọi nên coi đây là lỗi cấu hình, KHÔNG âm thầm bỏ qua)."""

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dependency_evidence'"
    ).fetchone()
    if table_exists is None:
        if not create_if_missing:
            raise RuntimeError(
                "Bảng dependency_evidence không tồn tại — bảng này thuộc Batch "
                "Orchestrator/Tier 1, orchestrator_hook không tự tạo trừ khi "
                "create_if_missing=True (chỉ dùng cho demo/test độc lập)."
            )
        conn.executescript(_MINIMAL_DDL)

    _ensure_tier2_columns(conn)

    where_clause = " AND ".join(f"{col} = ?" for col in _IDENTITY_COLUMNS)
    params = [getattr(escalation, col) for col in _IDENTITY_COLUMNS]

    cur = conn.execute(
        f"UPDATE dependency_evidence SET tier2_status = ?, tier2_original_status = ? "
        f"WHERE run_id = ? AND {where_clause}",
        [result.final_status, result.original_agent_status, run_id, *params],
    )
    conn.commit()
    return cur.rowcount
