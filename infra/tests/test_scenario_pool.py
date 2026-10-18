"""scenario_pool.py — sinh kịch bản cấy cho R=15 trial (PROPOSAL.md mục 7)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scenarios"))

from scenario_pool import (  # noqa: E402
    ONLINE_BOUTIQUE_REAL_EDGES,
    ONLINE_BOUTIQUE_SERVICES,
    ScenarioKind,
    generate_all_trials,
    generate_shared_trials,
    generate_trial,
)


def test_generate_trial_deterministic_with_same_seed():
    a = generate_trial("trial_01", seed=42)
    b = generate_trial("trial_01", seed=42)
    assert [e.pair_key() for e in a.injected_edges] == [e.pair_key() for e in b.injected_edges]
    assert [e.kind for e in a.injected_edges] == [e.kind for e in b.injected_edges]


def test_generate_trial_deterministic_from_trial_id_alone():
    a = generate_trial("trial_05")
    b = generate_trial("trial_05")
    assert a.seed == b.seed
    assert [e.pair_key() for e in a.injected_edges] == [e.pair_key() for e in b.injected_edges]


def test_generate_trial_has_all_six_kinds_exactly_once():
    scenario = generate_trial("trial_01", seed=1)
    kinds = [e.kind for e in scenario.injected_edges]
    assert set(kinds) == set(ScenarioKind)
    assert len(kinds) == len(set(kinds)) == 6


def test_injected_edges_never_duplicate_real_edges():
    real = set(ONLINE_BOUTIQUE_REAL_EDGES)
    for seed in range(20):
        scenario = generate_trial(f"trial_seed_{seed}", seed=seed)
        for edge in scenario.injected_edges:
            assert (edge.source, edge.destination) not in real


def test_sparse_hidden_within_muc_13_3_definition():
    """mục 13.3: traffic thưa = distinct_days <= 2."""
    for seed in range(10):
        scenario = generate_trial(f"trial_{seed}", seed=seed)
        sparse = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.SPARSE_HIDDEN)
        assert sparse.active_days_out_of_window <= 2
        assert sparse.is_real_dependency is True


def test_strong_hidden_is_real_and_frequent():
    scenario = generate_trial("trial_x", seed=7)
    strong = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.STRONG_HIDDEN)
    assert strong.is_real_dependency is True
    assert strong.requests_per_minute >= 20
    assert strong.active_days_out_of_window == 7


def test_config_only_clean_is_real_but_unresolved_is_not():
    scenario = generate_trial("trial_x", seed=7)
    clean = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.CONFIG_ONLY_CLEAN)
    unresolved = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.CONFIG_ONLY_UNRESOLVED)
    assert clean.is_real_dependency is True
    assert clean.config_reference_resolvable is True
    assert unresolved.is_real_dependency is False
    assert unresolved.config_reference_resolvable is False
    # Cả 2 đều không sinh traffic thật (đúng bản chất "config-only")
    assert clean.requests_per_minute is None
    assert unresolved.requests_per_minute is None


def test_noise_edges_are_never_real_dependencies():
    scenario = generate_trial("trial_x", seed=7)
    for kind in (ScenarioKind.NOISE_HEALTHCHECK, ScenarioKind.NOISE_MONITORING):
        edge = next(e for e in scenario.injected_edges if e.kind == kind)
        assert edge.is_real_dependency is False


def test_noise_monitoring_source_is_a_known_monitoring_name():
    from scenario_pool import _MONITORING_SOURCES

    scenario = generate_trial("trial_x", seed=7)
    monitoring = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.NOISE_MONITORING)
    assert monitoring.source in _MONITORING_SOURCES


def test_noise_healthcheck_path_looks_like_healthcheck():
    scenario = generate_trial("trial_x", seed=7)
    hc = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.NOISE_HEALTHCHECK)
    assert any(p in hc.path for p in ("/health", "/healthz", "/readyz"))


def test_noise_healthcheck_destination_is_always_frontend():
    """classification.py chỉ nhận diện HEALTHCHECK qua `series.path` (áp dụng tầng HTTP).
    Nếu đích không phải "frontend" (service HTTP duy nhất), harness không có cách nào
    tạo traffic thật mang đúng path đó -> cạnh sẽ không được thu đúng làm HEALTHCHECK
    khi chạy trên cluster thật. Đích phải luôn là "frontend"."""
    for seed in range(10):
        scenario = generate_trial(f"trial_{seed}", seed=seed)
        hc = next(e for e in scenario.injected_edges if e.kind == ScenarioKind.NOISE_HEALTHCHECK)
        assert hc.destination == "frontend"
        assert hc.source != "frontend"
        assert hc.destination_port_protocol[1] == "HTTP"


def test_generate_all_trials_ids_are_valid_k8s_names():
    """trial_id dùng trực tiếp làm tên namespace K8s (render_manifests.py::render()) ->
    PHẢI là RFC 1123 label hợp lệ (chữ thường/số/dấu gạch ngang), không được chứa "_".
    kubectl từ chối thẳng "trial_01" với lỗi 'must be a lowercase RFC 1123 label'."""
    import re

    rfc1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    trials = generate_all_trials(n_trials=15)
    for t in trials:
        assert rfc1123.match(t.trial_id), t.trial_id


def test_ground_truth_includes_baseline_real_edges_as_true():
    scenario = generate_trial("trial_x", seed=7)
    gt = scenario.ground_truth()
    for s, d in ONLINE_BOUTIQUE_REAL_EDGES:
        assert gt[("default", s, "default", d)] is True


def test_ground_truth_labels_match_each_injected_edge():
    scenario = generate_trial("trial_x", seed=7)
    gt = scenario.ground_truth()
    for edge in scenario.injected_edges:
        assert gt[edge.pair_key()] == edge.is_real_dependency


def test_generate_all_trials_distinct_ids_and_varied_scenarios():
    trials = generate_all_trials(n_trials=15, base_seed=1000)
    assert len(trials) == 15
    assert len({t.trial_id for t in trials}) == 15

    # Không phải mọi trial chọn TRÙNG hệt cùng 1 tổ hợp cạnh (đa dạng kịch bản, mục 6
    # TRAFFIC_COLLECTION_PIPELINE.md: "mỗi lần cấy một kịch bản khác nhau")
    strong_edges_per_trial = {
        t.trial_id: next(e for e in t.injected_edges if e.kind == ScenarioKind.STRONG_HIDDEN).pair_key()
        for t in trials
    }
    assert len(set(strong_edges_per_trial.values())) > 1


def test_generate_all_trials_reproducible_across_calls():
    a = generate_all_trials(n_trials=5, base_seed=999)
    b = generate_all_trials(n_trials=5, base_seed=999)
    for ta, tb in zip(a, b):
        assert [e.pair_key() for e in ta.injected_edges] == [e.pair_key() for e in tb.injected_edges]


# ---------------------------------------------------------------------------
# generate_shared_trials() — kiến trúc "dùng chung hạ tầng" (README "Việc còn thiếu",
# giải pháp capacity đã chọn cùng cô Dung sau khi check_capacity.sh xác nhận THẬT model
# namespace-riêng chỉ chạy nổi tối đa 4/15 trial đồng thời trên máy Bin, 2026-09-27).
# ---------------------------------------------------------------------------


def test_generate_shared_trials_every_virtual_trial_has_all_6_kinds():
    scenario = generate_shared_trials(n_trials=15, base_seed=1)
    assert len(scenario.virtual_trial_ids) == 15
    for vtid in scenario.virtual_trial_ids:
        edges = scenario.edges_for(vtid)
        assert len(edges) == 6, (vtid, len(edges))
        assert {e.kind for e in edges} == set(ScenarioKind)


def test_generate_shared_trials_deployment_keys_are_globally_unique():
    """Bất biến QUAN TRỌNG NHẤT của model dùng chung: 2 harness Deployment không được
    trùng (kind, source, destination) -- trùng sẽ trùng TÊN K8s thật
    (render_manifests.py::_harness_deployment_yaml), `kubectl apply` sẽ ghi đè thay vì
    tạo 2 pod độc lập. generate_shared_trials() khử trùng lặp bằng cách gộp vào chung 1
    SharedEdge (owners nhiều hơn 1) thay vì tạo 2 edge riêng có cùng key -- test này xác
    nhận invariant đó, KHÔNG xác nhận "không bao giờ có cạnh chia sẻ" (điều đó SAI, xem
    docstring generate_shared_trials -- NOISE_HEALTHCHECK/NOISE_MONITORING có thể chia sẻ)."""
    scenario = generate_shared_trials(n_trials=15, base_seed=1)
    keys = [se.dedup_key for se in scenario.shared_edges]
    assert len(keys) == len(set(keys))


def test_generate_shared_trials_noise_healthcheck_must_share_owners_when_pool_exhausted():
    """Xem docstring generate_shared_trials: pool nguồn cho NOISE_HEALTHCHECK (đích luôn
    "frontend") chỉ có 10 service -- với 15 virtual trial, PHẢI có ít nhất 1 cạnh có >1
    owner (nguyên lý Dirichlet: 15 lượt chọn nhét vào <=10 giá trị khác nhau)."""
    scenario = generate_shared_trials(n_trials=15, base_seed=1)
    hc_edges = [se for se in scenario.shared_edges if se.edge.kind == ScenarioKind.NOISE_HEALTHCHECK]
    assert len(hc_edges) <= len(ONLINE_BOUTIQUE_SERVICES) - 1
    assert sum(len(se.owners) for se in hc_edges) == 15  # mỗi virtual trial vẫn "chọn" đúng 1 cạnh HC
    assert any(len(se.owners) > 1 for se in hc_edges)


def test_generate_shared_trials_owner_map_covers_every_shared_edge():
    scenario = generate_shared_trials(n_trials=15, base_seed=1, namespace="shared-eval")
    om = scenario.owner_map()
    assert len(om) == len(scenario.shared_edges)
    for se in scenario.shared_edges:
        key = se.edge.pair_key("shared-eval")
        assert set(om[key]) == set(se.owners)


def test_generate_shared_trials_ground_truth_only_includes_own_edges_not_others():
    """Ground truth của virtual trial X KHÔNG được lẫn cạnh cấy CHỈ thuộc virtual trial
    khác -- nếu lẫn, 08_export_trial_data.py sẽ tính nhầm traffic của trial khác thành
    bằng chứng/FP cho trial X (xem NOTE trong split_shared_collection)."""
    scenario = generate_shared_trials(n_trials=15, base_seed=1, namespace="shared-eval")
    gt_1 = scenario.ground_truth_for("trial-01")
    gt_2 = scenario.ground_truth_for("trial-02")

    edges_only_in_2 = [
        se.edge for se in scenario.shared_edges
        if se.owners == ["trial-02"]  # thuộc DUY NHẤT trial-02, không chia sẻ
    ]
    assert edges_only_in_2, "cần ít nhất 1 cạnh không chia sẻ để test có ý nghĩa"
    for edge in edges_only_in_2:
        assert edge.pair_key("shared-eval") not in gt_1
        assert edge.pair_key("shared-eval") in gt_2


def test_generate_shared_trials_reproducible_across_calls():
    a = generate_shared_trials(n_trials=15, base_seed=555)
    b = generate_shared_trials(n_trials=15, base_seed=555)
    assert {se.dedup_key: tuple(se.owners) for se in a.shared_edges} == {
        se.dedup_key: tuple(se.owners) for se in b.shared_edges
    }


def test_generate_shared_trials_raises_when_pool_too_small_for_n_trials():
    # 5 vai trò không chia sẻ x N trial > pool ~85 cặp -- phải báo lỗi RÕ RÀNG, không âm
    # thầm giảm số cạnh (khác NOISE_HEALTHCHECK, xem docstring).
    import pytest

    with pytest.raises(ValueError, match="pool chỉ có"):
        generate_shared_trials(n_trials=100)
