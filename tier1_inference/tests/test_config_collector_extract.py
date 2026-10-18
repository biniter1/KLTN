"""config_collector/extract.py — bước [4] LLM EXTRACTION (CONFIG_EVIDENCE_COLLECTOR.md
mục 4.3) -- FakeLLMClient/InMemoryExtractionCache, không gọi API thật."""

from __future__ import annotations

import pytest

from tier1.config_collector.extract import (
    DEFAULT_PROMPT_VERSION,
    ExtractionFailed,
    FakeLLMClient,
    InMemoryExtractionCache,
    LLMExtractionError,
    compute_cache_key,
    extract_all,
    extract_chunk,
)
from tier1.config_collector.funnel import Chunk, FunnelItem

NS = "default"


def _funnel_item(item_id="i0000", key="ADDR", value="cartservice:7070") -> FunnelItem:
    return FunnelItem(
        item_id=item_id, workload="checkoutservice", namespace=NS, container="server",
        container_type="app", source_kind="env", key=key, value=value, redacted_value=value,
        funnel_status="KEPT", source_ref=None,
        rendered_block=f'<config_item id="{item_id}" workload="checkoutservice" container="server" '
                        f'source="env" key="{key}">\n{value}\n</config_item>',
    )


def _chunk(items=None, chunk_index=0, chunk_count=1, workload="checkoutservice") -> Chunk:
    items = items if items is not None else [_funnel_item()]
    return Chunk(
        workload=workload, namespace=NS, chunk_index=chunk_index, chunk_count=chunk_count,
        items=tuple(items), text="\n".join(it.rendered_block for it in items),
    )


_VALID_RESPONSE = {
    "references": [
        {"item_id": "i0000", "quote": "cartservice:7070", "host": "cartservice", "port": 7070,
         "scheme": None, "port_item_id": None, "port_quote": None},
    ]
}


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_extract_chunk_returns_parsed_references():
    client = FakeLLMClient(responses=[_VALID_RESPONSE])
    refs = extract_chunk(_chunk(), client)
    assert len(refs) == 1
    assert refs[0].item_id == "i0000"
    assert refs[0].host == "cartservice"
    assert refs[0].port == 7070


def test_extract_chunk_accepts_minimal_required_fields_only():
    client = FakeLLMClient(responses=[{"references": [{"item_id": "i0000", "quote": "x", "host": None, "port": None}]}])
    refs = extract_chunk(_chunk(), client)
    assert refs[0].scheme is None and refs[0].port_item_id is None and refs[0].port_quote is None


def test_extract_chunk_empty_references_list_is_valid():
    client = FakeLLMClient(responses=[{"references": []}])
    refs = extract_chunk(_chunk(), client)
    assert refs == []


# ---------------------------------------------------------------------------
# schema retry / failure
# ---------------------------------------------------------------------------


def test_schema_invalid_then_valid_on_retry_succeeds():
    client = FakeLLMClient(responses=[{"references": "not-a-list"}, _VALID_RESPONSE])
    refs = extract_chunk(_chunk(), client)
    assert len(refs) == 1
    assert len(client.calls) == 2


def test_schema_invalid_twice_raises_extraction_failed_not_fallback():
    client = FakeLLMClient(responses=[{"bad": True}, {"also_bad": True}])
    with pytest.raises(ExtractionFailed):
        extract_chunk(_chunk(), client)
    assert len(client.calls) == 2


def test_missing_required_field_is_schema_error():
    client = FakeLLMClient(responses=[{"references": [{"item_id": "i0000", "quote": "x"}]}, _VALID_RESPONSE])
    refs = extract_chunk(_chunk(), client)
    assert len(refs) == 1  # lần đầu thiếu host/port -> retry -> lần 2 hợp lệ


def test_wrong_type_for_port_is_schema_error():
    bad = {"references": [{"item_id": "i0000", "quote": "x", "host": "h", "port": "not-an-int"}]}
    client = FakeLLMClient(responses=[bad, bad])
    with pytest.raises(ExtractionFailed):
        extract_chunk(_chunk(), client)


def test_api_error_is_not_retried_and_propagates():
    client = FakeLLMClient(responses=[LLMExtractionError("network down"), _VALID_RESPONSE])
    with pytest.raises(LLMExtractionError):
        extract_chunk(_chunk(), client)
    # Không retry cho lỗi API -- chỉ gọi đúng 1 lần.
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_second_api_call():
    cache = InMemoryExtractionCache()
    client = FakeLLMClient(responses=[_VALID_RESPONSE])
    chunk = _chunk()

    refs1 = extract_chunk(chunk, client, cache=cache)
    refs2 = extract_chunk(chunk, client, cache=cache)

    assert refs1 == refs2
    assert len(client.calls) == 1  # lần 2 trúng cache, không gọi lại API


def test_cache_key_depends_on_prompt_version_model_and_chunk_text():
    k1 = compute_cache_key("v1", "model-a", "text-A")
    k2 = compute_cache_key("v2", "model-a", "text-A")
    k3 = compute_cache_key("v1", "model-b", "text-A")
    k4 = compute_cache_key("v1", "model-a", "text-B")
    assert len({k1, k2, k3, k4}) == 4


def test_cache_key_is_deterministic():
    assert compute_cache_key("v1", "m", "t") == compute_cache_key("v1", "m", "t")


def test_different_chunks_get_different_cache_entries():
    cache = InMemoryExtractionCache()
    client = FakeLLMClient(responses=[
        {"references": [{"item_id": "i0000", "quote": "a:1", "host": "a", "port": 1}]},
        {"references": [{"item_id": "i0001", "quote": "b:2", "host": "b", "port": 2}]},
    ])
    chunk_a = _chunk(items=[_funnel_item(item_id="i0000", key="A", value="a:1")])
    chunk_b = _chunk(items=[_funnel_item(item_id="i0001", key="B", value="b:2")])

    refs_a = extract_chunk(chunk_a, client, cache=cache)
    refs_b = extract_chunk(chunk_b, client, cache=cache)

    assert refs_a[0].host == "a"
    assert refs_b[0].host == "b"
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# extract_all — dừng ngay khi 1 chunk fail (không tiếp tục các chunk còn lại)
# ---------------------------------------------------------------------------


def test_extract_all_runs_every_chunk_when_all_succeed():
    client = FakeLLMClient(responses=[_VALID_RESPONSE, {"references": []}])
    chunks = [_chunk(chunk_index=0), _chunk(chunk_index=1)]
    results = extract_all(chunks, client)
    assert len(results) == 2


def test_extract_all_stops_on_first_failure():
    client = FakeLLMClient(responses=[{"bad": True}, {"bad": True}, _VALID_RESPONSE])
    chunks = [_chunk(chunk_index=0), _chunk(chunk_index=1)]
    with pytest.raises(ExtractionFailed):
        extract_all(chunks, client)
    # Chunk thứ 2 không được gọi vì chunk đầu đã fail và dừng ngay.
    assert len(client.calls) == 2


def test_default_prompt_version_is_fixed_string():
    assert isinstance(DEFAULT_PROMPT_VERSION, str) and DEFAULT_PROMPT_VERSION
