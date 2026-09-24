"""storage.py — SQLite WAL, roundtrip previous_status (mục 5b) và đếm flip (unstable_classification)."""

from datetime import datetime, timedelta, timezone

from tier1 import storage
from tier1.schema import (
    DependencyEdgeResult,
    DependencyStatus,
    Scope,
    TrafficClass,
)

from .conftest import make_identity_key


def _ts(days_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _result(key, status, score=0.9) -> DependencyEdgeResult:
    return DependencyEdgeResult(
        identity_key=key,
        destination_service_ref=None,
        scope=Scope.IN_CLUSTER,
        traffic_class=TrafficClass.BUSINESS,
        count=100,
        distinct_days=5,
        error_ratio=0.0,
        unstable=False,
        config_finding=None,
        score=score,
        dependency_status=status,
        snapshot_id="snap_test",
    )


def test_connect_creates_schema_and_wal(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    conn = storage.connect(db_path)
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "dependency_evidence" in tables
    conn.close()


def test_insert_and_fetch_previous_status_roundtrip(tmp_path):
    conn = storage.connect(tmp_path / "t.sqlite3")
    key = make_identity_key(dest="productcatalogservice")
    storage.insert_results(conn, [_result(key, DependencyStatus.OBSERVED_STRONG)])

    prev = storage.get_previous_status_map(conn, [key])
    assert prev[key] == DependencyStatus.OBSERVED_STRONG
    conn.close()


def test_previous_status_missing_for_unknown_key(tmp_path):
    conn = storage.connect(tmp_path / "t.sqlite3")
    key = make_identity_key(dest="never-inserted")
    prev = storage.get_previous_status_map(conn, [key])
    assert key not in prev
    conn.close()


def test_previous_status_picks_latest_row(tmp_path):
    conn = storage.connect(tmp_path / "t.sqlite3")
    key = make_identity_key(dest="svc")
    storage.insert_results(conn, [_result(key, DependencyStatus.OBSERVED_WEAK)], run_timestamp=_ts(5))
    storage.insert_results(conn, [_result(key, DependencyStatus.OBSERVED_STRONG)], run_timestamp=_ts(1))
    prev = storage.get_previous_status_map(conn, [key])
    assert prev[key] == DependencyStatus.OBSERVED_STRONG  # dòng gần nhất (1 ngày trước), không phải 5 ngày
    conn.close()


def test_recent_flip_count_only_counts_transitions_inside_window(tmp_path):
    conn = storage.connect(tmp_path / "t.sqlite3")
    key = make_identity_key(dest="flaky-svc")

    # -10 ngày: STRONG (NGOÀI cửa sổ 7 ngày -- không tính transition dẫn vào đây)
    storage.insert_results(conn, [_result(key, DependencyStatus.OBSERVED_STRONG)], run_timestamp=_ts(10))
    # -6, -3, -1 ngày: WEAK, STRONG, WEAK (TRONG cửa sổ) -> 2 lần đổi giữa 3 dòng này
    storage.insert_results(conn, [_result(key, DependencyStatus.OBSERVED_WEAK)], run_timestamp=_ts(6))
    storage.insert_results(conn, [_result(key, DependencyStatus.OBSERVED_STRONG)], run_timestamp=_ts(3))
    storage.insert_results(conn, [_result(key, DependencyStatus.OBSERVED_WEAK)], run_timestamp=_ts(1))

    flips = storage.get_recent_flip_count(conn, key, window_days=7)
    assert flips == 2
    conn.close()


def test_recent_flip_count_reaches_unstable_threshold(tmp_path):
    conn = storage.connect(tmp_path / "t.sqlite3")
    key = make_identity_key(dest="very-flaky-svc")
    statuses = [
        DependencyStatus.OBSERVED_WEAK, DependencyStatus.OBSERVED_STRONG,
        DependencyStatus.OBSERVED_WEAK, DependencyStatus.OBSERVED_STRONG,
    ]
    for i, status in enumerate(statuses):
        days_ago = 6 - 2 * i  # 6, 4, 2, 0 ngày trước -- tất cả trong cửa sổ 7 ngày
        storage.insert_results(conn, [_result(key, status)], run_timestamp=_ts(days_ago))

    flips = storage.get_recent_flip_count(conn, key, window_days=7)
    assert flips == 3  # 4 dòng liên tiếp khác nhau từng đôi một -> 3 lần đổi
    conn.close()


def test_recent_flip_count_no_history_is_zero(tmp_path):
    conn = storage.connect(tmp_path / "t.sqlite3")
    key = make_identity_key(dest="brand-new-svc")
    assert storage.get_recent_flip_count(conn, key, window_days=7) == 0
    conn.close()


def test_insert_results_persists_bookkeeping_fields(tmp_path):
    conn = storage.connect(tmp_path / "t.sqlite3")
    key = make_identity_key(dest="svc")
    result = _result(key, DependencyStatus.OBSERVED_WEAK)
    result.escalate_to_tier2 = True
    result.escalation_reason = "test reason"
    result.needs_human_review = False
    storage.insert_results(conn, [result])

    rows = storage.fetch_latest_snapshot(conn, "snap_test")
    assert len(rows) == 1
    row = rows[0]
    assert row["escalate_to_tier2"] == 1
    assert row["escalation_reason"] == "test reason"
    assert row["dependency_status"] == "OBSERVED_WEAK"
    conn.close()
