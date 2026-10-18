"""config_collector/pipeline.py + store.py — điều phối toàn bộ pipeline v2
(CONFIG_EVIDENCE_COLLECTOR.md mục 4, 6) end-to-end, FakeK8sReader + FakeLLMClient,
không cần cluster/API thật."""

from __future__ import annotations

import sqlite3

from tier1.config_collector.extract import FakeLLMClient
from tier1.config_collector.k8s_types import ContainerSpec, FakeK8sReader, PodInfo, ServiceInfo, ServicePortInfo
from tier1.config_collector.pipeline import collect_config_evidence
from tier1.schema import ConfigFinding, Scope

NS = "trial-01"


def _pods_and_service():
    source = PodInfo(
        name="checkoutservice", namespace=NS, service_account_name="checkoutservice",
        labels={"app": "checkoutservice"},
        containers=[ContainerSpec(name="server", env={"CART_SERVICE_ADDR": "cartservice:7070"})],
    )
    dest = PodInfo(name="cartservice", namespace=NS, service_account_name="cartservice", labels={"app": "cartservice"})
    svc = ServiceInfo(name="cartservice", namespace=NS, selector={"app": "cartservice"},
                       ports=[ServicePortInfo(port=7070, target_port=7070, protocol="TCP")])
    return [source, dest], [svc]


_VALID_RESPONSE = {
    "references": [
        {"item_id": "i0000", "quote": "cartservice:7070", "host": "cartservice", "port": 7070,
         "scheme": None, "port_item_id": None, "port_quote": None},
    ]
}


def test_collect_config_evidence_end_to_end_in_memory():
    pods, services = _pods_and_service()
    reader = FakeK8sReader(pods=pods, services=services)
    llm_client = FakeLLMClient(responses=[_VALID_RESPONSE])

    records = collect_config_evidence(
        reader, snapshot_id="trial-01-snapshot", namespaces=[NS], llm_client=llm_client,
    )

    assert len(records) == 1
    rec = records[0]
    assert rec.source_canonical_service == "checkoutservice"
    assert rec.destination_canonical_service == "cartservice"
    assert rec.port == 7070
    assert rec.config_finding == ConfigFinding.FOUND_STRUCTURAL
    assert rec.scope == Scope.IN_CLUSTER
    assert rec.extraction_method == "llm"
    assert rec.run_id == "trial-01-snapshot"
    assert rec.model_id == "fake-model"


def test_collect_config_evidence_backward_compatible_call_signature():
    """Lệnh gọi CŨ của 08_export_trial_data.py -- không truyền bất kỳ tham số mới nào."""
    pods, services = _pods_and_service()
    reader = FakeK8sReader(pods=pods, services=services)
    llm_client = FakeLLMClient(responses=[_VALID_RESPONSE])
    records = collect_config_evidence(reader, snapshot_id="trial-02-snapshot", namespaces=[NS], llm_client=llm_client)
    assert isinstance(records, list)
    assert len(records) == 1


def test_collect_config_evidence_no_db_path_writes_nothing_to_disk(tmp_path):
    pods, services = _pods_and_service()
    reader = FakeK8sReader(pods=pods, services=services)
    llm_client = FakeLLMClient(responses=[_VALID_RESPONSE])
    records = collect_config_evidence(reader, snapshot_id="s1", namespaces=[NS], llm_client=llm_client)
    assert len(records) == 1  # không truyền db_path -- chỉ trả record trong bộ nhớ, không lỗi


def test_collect_config_evidence_writes_all_five_tables_when_db_path_given(tmp_path):
    db_path = tmp_path / "trial.sqlite"
    pods, services = _pods_and_service()
    reader = FakeK8sReader(pods=pods, services=services)
    llm_client = FakeLLMClient(responses=[_VALID_RESPONSE])

    records = collect_config_evidence(
        reader, snapshot_id="trial-01-snapshot", namespaces=[NS], llm_client=llm_client,
        run_id="trial-01", db_path=db_path,
    )
    assert len(records) == 1

    conn = sqlite3.connect(str(db_path))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"config_raw_items", "llm_extraction_cache", "config_extractions",
            "config_evidence", "collection_quality_log"} <= tables

    raw_count = conn.execute("SELECT COUNT(*) FROM config_raw_items WHERE run_id='trial-01'").fetchone()[0]
    assert raw_count > 0

    extraction_count = conn.execute("SELECT COUNT(*) FROM config_extractions WHERE run_id='trial-01'").fetchone()[0]
    assert extraction_count == 1

    evidence_rows = conn.execute(
        "SELECT destination_canonical_service, config_finding, target_port FROM config_evidence WHERE run_id='trial-01'"
    ).fetchall()
    assert evidence_rows == [("cartservice", "FOUND_STRUCTURAL", 7070)]

    conn.row_factory = sqlite3.Row
    quality_rows = conn.execute(
        "SELECT * FROM collection_quality_log WHERE run_id='trial-01'"
    ).fetchall()
    assert len(quality_rows) == 1  # DẠNG NGANG (DASHBOARD_DESIGN.md mục 3.A): 1 dòng/module
    q = quality_rows[0]
    assert q["module"] == "config_collector"
    assert q["evidence_total"] == 1
    assert q["candidates_found"] == 1
    assert q["llm_calls"] == 1
    assert q["llm_cache_hits"] == 0
    assert q["refs_extracted"] == 1


def test_sqlite_cache_persists_across_separate_collect_calls(tmp_path):
    """mục 2: cache BỀN qua nhiều lần gọi khác nhau -- config không đổi thì không gọi
    LLM lại lần thứ 2."""
    db_path = tmp_path / "trial.sqlite"
    pods, services = _pods_and_service()

    reader1 = FakeK8sReader(pods=pods, services=services)
    client1 = FakeLLMClient(responses=[_VALID_RESPONSE])
    collect_config_evidence(reader1, snapshot_id="s1", namespaces=[NS], llm_client=client1,
                             run_id="run1", db_path=db_path)
    assert len(client1.calls) == 1

    reader2 = FakeK8sReader(pods=pods, services=services)
    client2 = FakeLLMClient(responses=[])  # không cho response nào -- phải trúng cache, không gọi
    records2 = collect_config_evidence(reader2, snapshot_id="s2", namespaces=[NS], llm_client=client2,
                                        run_id="run2", db_path=db_path)
    assert len(client2.calls) == 0
    assert len(records2) == 1


def test_collect_config_evidence_no_evidence_when_llm_returns_empty():
    pods, services = _pods_and_service()
    reader = FakeK8sReader(pods=pods, services=services)
    llm_client = FakeLLMClient(responses=[{"references": []}])
    records = collect_config_evidence(reader, snapshot_id="s1", namespaces=[NS], llm_client=llm_client)
    assert records == []


def test_collect_config_evidence_unresolved_destination_has_none_identity():
    source = PodInfo(
        name="checkoutservice", namespace=NS, service_account_name="checkoutservice",
        labels={"app": "checkoutservice"},
        containers=[ContainerSpec(name="server", env={"ADDR": "totallyunknownhost:1234"})],
    )
    reader = FakeK8sReader(pods=[source])
    llm_client = FakeLLMClient(responses=[{
        "references": [{"item_id": "i0000", "quote": "totallyunknownhost:1234",
                         "host": "totallyunknownhost", "port": 1234,
                         "scheme": None, "port_item_id": None, "port_quote": None}],
    }])
    records = collect_config_evidence(reader, snapshot_id="s1", namespaces=[NS], llm_client=llm_client)
    assert len(records) == 1
    assert records[0].config_finding == ConfigFinding.FOUND_UNRESOLVED
    assert records[0].destination_canonical_service is None
    assert records[0].raw_host == "totallyunknownhost"


# ---------------------------------------------------------------------------
# collection_quality_log dạng ngang (DASHBOARD_DESIGN.md mục 3.A) + bộ đếm LLM
# ---------------------------------------------------------------------------


def test_stats_out_filled_with_section_3a_column_names():
    pods, services = _pods_and_service()
    stats: dict = {}
    collect_config_evidence(
        FakeK8sReader(pods=pods, services=services), snapshot_id="s", namespaces=[NS],
        llm_client=FakeLLMClient(responses=[_VALID_RESPONSE]), stats_out=stats,
    )
    assert stats["workloads_scanned"] == 2
    assert stats["chunks_sent"] == 1
    assert stats["llm_calls"] == 1
    assert set(stats["items_dropped_by_funnel"]) == {"EMPTY", "BOOLEAN", "BINARY", "SECRET_REF"}
    assert stats["candidates_found"] == 1
    assert stats["candidates_found_unresolved"] == 0


def test_llm_cache_hit_counted_on_second_call(tmp_path):
    db_path = tmp_path / "trial.sqlite"
    pods, services = _pods_and_service()
    collect_config_evidence(FakeK8sReader(pods=pods, services=services), snapshot_id="s1", namespaces=[NS],
                            llm_client=FakeLLMClient(responses=[_VALID_RESPONSE]), run_id="r1", db_path=db_path)
    stats: dict = {}
    collect_config_evidence(FakeK8sReader(pods=pods, services=services), snapshot_id="s2", namespaces=[NS],
                            llm_client=FakeLLMClient(responses=[]), run_id="r2", db_path=db_path, stats_out=stats)
    assert stats["llm_calls"] == 0
    assert stats["llm_cache_hits"] == 1


def test_write_quality_log_false_skips_row_but_still_writes_other_tables(tmp_path):
    db_path = tmp_path / "trial.sqlite"
    pods, services = _pods_and_service()
    collect_config_evidence(FakeK8sReader(pods=pods, services=services), snapshot_id="s", namespaces=[NS],
                            llm_client=FakeLLMClient(responses=[_VALID_RESPONSE]), run_id="r",
                            db_path=db_path, write_quality_log=False)
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM collection_quality_log").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM config_evidence").fetchone()[0] == 1


def test_legacy_vertical_quality_log_renamed_not_deleted(tmp_path):
    from tier1.config_collector import store

    db_path = tmp_path / "old.sqlite"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        "CREATE TABLE collection_quality_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,"
        " metric TEXT NOT NULL, value REAL NOT NULL, created_at TEXT NOT NULL);"
        "CREATE INDEX idx_collection_quality_log_run ON collection_quality_log (run_id);"
        "INSERT INTO collection_quality_log (run_id, metric, value, created_at) VALUES ('t1','evidence_total',3,'x');"
    )
    old.commit()
    old.close()

    conn = store.connect(db_path)
    legacy = conn.execute("SELECT run_id, metric, value FROM collection_quality_log_legacy").fetchall()
    assert legacy == [("t1", "evidence_total", 3.0)]
    cols = {row[1] for row in conn.execute("PRAGMA table_info(collection_quality_log)")}
    assert "module" in cols and "metric" not in cols
    store.connect(db_path).close()  # idempotent — gọi lại không lỗi


def test_insert_quality_log_rejects_unknown_column(tmp_path):
    import pytest
    from tier1.config_collector import store

    conn = store.connect(tmp_path / "q.sqlite")
    with pytest.raises(ValueError):
        store.insert_quality_log(conn, "r", "config_collector", {"not_a_column": 1})
