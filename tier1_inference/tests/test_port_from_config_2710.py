"""Lấy port từ cấu hình cho cạnh chưa có port (chốt 27/10/2026).

Thứ tự nguồn port: observed -> observed_log -> inferred_single_port -> inferred_protocol_match
-> config -> unknown. Chỉ lấy khi bằng chứng cấu hình SẠCH của cặp có đúng một port."""

from tier1.pipeline import run_tier1
from tier1.schema import ConfigFinding

from .test_tier1_decisions_1510 import by_dest, ev, series

DAYS = [f"2026-10-0{d}" for d in range(1, 8)]


def _observed(results, dst="cartservice"):
    return [r for r in by_dest(results, dst) if r.count > 0]


def test_single_config_port_fills_unknown_port():
    results = run_tier1([series("frontend", "cartservice", port=None, port_source="unknown", days=DAYS)],
                        [ev("frontend", "cartservice", 7070)], "s")
    obs = _observed(results)
    assert len(obs) == 1
    r = obs[0]
    assert r.identity_key.port == 7070
    assert r.port_source == "config"
    assert "port_from_config" in r.flags and "port_unresolved" not in r.flags
    assert r.config_match_level == "EXACT"  # khớp đúng bản ghi config sau khi có port
    assert not [x for x in by_dest(results, "cartservice") if x.count == 0]  # config không thành dòng dư


def test_two_config_ports_do_not_guess():
    results = run_tier1([series("frontend", "cartservice", port=None, port_source="unknown")],
                        [ev("frontend", "cartservice", 7070, item_id="i1"),
                         ev("frontend", "cartservice", 7071, item_id="i2")], "s")
    r = _observed(results)[0]
    assert r.identity_key.port is None and r.port_source == "unknown"
    assert "port_unresolved" in r.flags


def test_unresolved_config_not_used_for_port():
    results = run_tier1([series("frontend", "cartservice", port=None, port_source="unknown")],
                        [ev("frontend", "cartservice", 7070, finding=ConfigFinding.FOUND_UNRESOLVED)], "s")
    r = _observed(results)[0]
    assert r.identity_key.port is None


def test_observed_port_never_overridden_by_config():
    results = run_tier1([series("frontend", "cartservice", port=8080)],
                        [ev("frontend", "cartservice", 7070)], "s")
    r = _observed(results)[0]
    assert r.identity_key.port == 8080 and r.port_source == "observed"


def test_fill_skipped_when_key_already_taken():
    results = run_tier1([series("frontend", "cartservice", port=7070, path="/a"),
                         series("frontend", "cartservice", port=None, port_source="unknown", path="/b")],
                        [ev("frontend", "cartservice", 7070)], "s")
    obs = _observed(results)
    ports = sorted((r.identity_key.port is None, r.identity_key.port or 0) for r in obs)
    assert ports == [(False, 7070), (True, 0)]
    none_edge = [r for r in obs if r.identity_key.port is None][0]
    assert "config_port_taken" in none_edge.flags
