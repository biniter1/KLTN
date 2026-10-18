"""config_collector/funnel.py — bước [3] PHỄU LỌC + CHUẨN HOÁ
(CONFIG_EVIDENCE_COLLECTOR.md mục 4.2)."""

from __future__ import annotations

from tier1.config_collector.funnel import (
    DEFAULT_CHUNK_CHAR_LIMIT,
    build_funnel_items,
    chunk_items_by_workload,
)
from tier1.config_collector.merge import EffectiveConfigItem

NS = "default"


def _item(key, value, source_kind="env", status="ACTIVE", is_binary=False, container="main", workload="app") -> EffectiveConfigItem:
    return EffectiveConfigItem(
        workload=workload, namespace=NS, container=container, container_type="app",
        source_kind=source_kind, key=key, value=value, status=status, is_binary=is_binary,
    )


# ---------------------------------------------------------------------------
# classification (chỉ 5 lý do loại, không được loại thêm)
# ---------------------------------------------------------------------------


def test_kept_for_ordinary_value():
    items = build_funnel_items([_item("ADDR", "cartservice:7070")])
    assert items[0].funnel_status == "KEPT"
    assert items[0].redacted_value == "cartservice:7070"


def test_empty_value_dropped():
    items = build_funnel_items([_item("FLAG", "")])
    assert items[0].funnel_status == "EMPTY"


def test_boolean_value_case_insensitive_dropped():
    for raw in ["true", "FALSE", "Yes", "no", "ON", "off"]:
        items = build_funnel_items([_item("FLAG", raw)])
        assert items[0].funnel_status == "BOOLEAN", raw


def test_binary_flag_dropped_regardless_of_value_content():
    items = build_funnel_items([_item("cert.bin", "", is_binary=True)])
    assert items[0].funnel_status == "BINARY"


def test_secret_ref_dropped():
    items = build_funnel_items([_item("<secretKeyRef#0>", "<redacted>", source_kind="secret_ref")])
    assert items[0].funnel_status == "SECRET_REF"


def test_overridden_status_dropped():
    items = build_funnel_items([_item("ADDR", "old:1234", status="OVERRIDDEN")])
    assert items[0].funnel_status == "OVERRIDDEN"


def test_bare_number_and_short_string_are_not_dropped():
    items = build_funnel_items([_item("TIMEOUT", "30"), _item("CODE", "ab")])
    assert {it.funnel_status for it in items} == {"KEPT"}


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_redacts_userinfo_password_in_url():
    items = build_funnel_items([_item("DB_URL", "redis://user:s3cr3t@cache:6379/0")])
    assert items[0].redacted_value == "redis://user:****@cache:6379/0"
    # giá trị gốc (chưa redact) vẫn giữ nguyên để truy vết nội bộ.
    assert items[0].value == "redis://user:s3cr3t@cache:6379/0"


def test_redacts_password_key_value_pair():
    items = build_funnel_items([_item("JDBC", "jdbc:postgresql://db:5432/app?password=hunter2&user=svc")])
    assert "password=****" in items[0].redacted_value
    assert "hunter2" not in items[0].redacted_value


def test_rendered_block_uses_redacted_value_not_original():
    items = build_funnel_items([_item("DB_URL", "redis://user:s3cr3t@cache:6379/0")])
    assert "s3cr3t" not in items[0].rendered_block
    assert "cache:6379" in items[0].rendered_block


def test_dropped_items_have_empty_rendered_block_and_redacted_value():
    items = build_funnel_items([_item("FLAG", "")])
    assert items[0].rendered_block == ""
    assert items[0].redacted_value == ""


# ---------------------------------------------------------------------------
# item_id — ổn định, theo (workload, container, source_kind, key)
# ---------------------------------------------------------------------------


def test_item_id_assignment_is_stable_sorted_order():
    items = build_funnel_items([
        _item("Z_VAR", "z", container="b"),
        _item("A_VAR", "a", container="a"),
    ])
    by_key = {it.key: it.item_id for it in items}
    # container "a" < "b" -> A_VAR (container a) phải có item_id nhỏ hơn Z_VAR (container b)
    assert by_key["A_VAR"] < by_key["Z_VAR"]


def test_item_ids_are_unique():
    items = build_funnel_items([_item(f"K{i}", f"v{i}") for i in range(5)])
    assert len({it.item_id for it in items}) == 5


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def test_single_small_workload_produces_one_chunk():
    items = build_funnel_items([_item("A", "1"), _item("B", "2")])
    chunks = chunk_items_by_workload(items)
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0 and chunks[0].chunk_count == 1
    assert len(chunks[0].items) == 2


def test_dropped_items_excluded_from_chunks():
    items = build_funnel_items([_item("A", "1"), _item("EMPTY", "")])
    chunks = chunk_items_by_workload(items)
    assert len(chunks) == 1
    assert len(chunks[0].items) == 1
    assert chunks[0].items[0].key == "A"


def test_oversized_workload_splits_without_cutting_an_item():
    big_value = "x" * (DEFAULT_CHUNK_CHAR_LIMIT - 50)
    items = build_funnel_items([
        _item("BIG1", big_value, workload="app"),
        _item("BIG2", big_value, workload="app"),
        _item("SMALL", "tiny", workload="app"),
    ])
    chunks = chunk_items_by_workload(items, char_limit=DEFAULT_CHUNK_CHAR_LIMIT)
    assert len(chunks) >= 2
    # mọi item chỉ xuất hiện NGUYÊN VẸN trong đúng 1 chunk, không bị cắt.
    all_keys_seen = [it.key for c in chunks for it in c.items]
    assert sorted(all_keys_seen) == ["BIG1", "BIG2", "SMALL"]
    for chunk in chunks:
        for it in chunk.items:
            assert it.rendered_block in chunk.text


def test_single_item_larger_than_limit_still_becomes_its_own_chunk():
    huge_value = "y" * (DEFAULT_CHUNK_CHAR_LIMIT * 2)
    items = build_funnel_items([_item("HUGE", huge_value)])
    chunks = chunk_items_by_workload(items, char_limit=DEFAULT_CHUNK_CHAR_LIMIT)
    assert len(chunks) == 1
    assert len(chunks[0].items) == 1
    assert chunks[0].items[0].key == "HUGE"


def test_chunks_are_scoped_per_workload_not_merged():
    items = build_funnel_items([
        _item("A", "1", workload="svc-a"),
        _item("B", "2", workload="svc-b"),
    ])
    chunks = chunk_items_by_workload(items)
    assert len(chunks) == 2
    assert {c.workload for c in chunks} == {"svc-a", "svc-b"}
