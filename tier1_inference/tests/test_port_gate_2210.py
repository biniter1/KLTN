"""Cờ port cho cổng ở cửa ra (chốt 22/10/2026): Tầng 1 KHÔNG chặn cạnh thiếu/mâu thuẫn port
(vẫn chấm điểm, vẫn có trạng thái) — chỉ gắn cờ để Planner/hàng đợi review chặn lúc sinh YAML."""

from tier1.pipeline import run_tier1
from tier1.schema import DependencyStatus

from .test_tier1_decisions_1510 import by_dest, ev, series


def test_unknown_port_still_scored_and_flagged():
    r = run_tier1([series("frontend", "cartservice", port=None, port_source="unknown", count=5000,
                          days=[f"2026-10-0{d}" for d in range(1, 8)])], [], "s")[0]
    assert r.identity_key.port is None
    assert "port_unresolved" in r.flags
    assert r.score is not None and r.dependency_status in (DependencyStatus.OBSERVED_STRONG,
                                                           DependencyStatus.OBSERVED_WEAK)


def test_known_port_not_flagged_unresolved():
    r = run_tier1([series("frontend", "cartservice", port=7070)], [], "s")[0]
    assert "port_unresolved" not in r.flags


def test_config_port_differs_flag_on_edge_and_leftover_kept():
    results = run_tier1([series("frontend", "cartservice", port=7070, port_source="inferred_single_port")],
                        [ev("frontend", "cartservice", 7071)], "s")
    observed = [r for r in by_dest(results, "cartservice") if r.count > 0]
    assert len(observed) == 1 and "config_port_differs" in observed[0].flags
    leftover = [r for r in by_dest(results, "cartservice") if r.count == 0]
    assert len(leftover) == 1 and leftover[0].identity_key.port == 7071  # config dư vẫn có dòng riêng


def test_config_port_equal_no_flag():
    r = run_tier1([series("frontend", "cartservice", port=7070)], [ev("frontend", "cartservice", 7070)], "s")[0]
    assert "config_port_differs" not in r.flags


def test_config_without_port_no_flag():
    r = run_tier1([series("frontend", "cartservice", port=7070)], [ev("frontend", "cartservice", None)], "s")[0]
    assert "config_port_differs" not in r.flags
