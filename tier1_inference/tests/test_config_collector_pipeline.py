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

    quality_rows = dict(
        conn.execute("SELECT metric, value FROM collection_quality_log WHERE run_id='trial-01'").fetchall()
    )
    assert quality_rows["evidence_total"] == 1
    assert quality_rows["found_structural"] == 1


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
