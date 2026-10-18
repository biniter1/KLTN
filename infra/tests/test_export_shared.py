"""split_shared_collection() trong 08_export_trial_data.py — phần THUẦN (không đụng K8s/
Prometheus thật) của chế độ "hạ tầng dùng chung" (05_deploy_shared.sh). Xác nhận đúng cơ
chế tách 1 lần thu thập traffic/config CHUNG thành N TrialInput riêng theo owner_map, KHÔNG
dùng nhãn Prometheus injection_id (đã xác nhận không hoạt động qua waypoint ambient, xem
TRAFFIC_COLLECTION_PIPELINE.md mục 3) — chỉ so khớp pair_key() thuần Python.
"""

import importlib.util
import json
import sys
from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parent.parent
TIER1_DIR = INFRA_DIR.parent / "tier1_inference"
sys.path.insert(0, str(TIER1_DIR))
sys.path.insert(0, str(INFRA_DIR / "scenarios"))

from tier1.schema import ConfigEvidenceRecord, ConfigFinding, Scope, TrafficSeries  # noqa: E402


def _load_export_module():
    """08_export_trial_data.py không phải tên module Python hợp lệ (bắt đầu bằng số) --
    nạp trực tiếp qua đường dẫn file, giống cách 05/06_*.sh gọi nó qua `python3 <path>`."""
    path = INFRA_DIR / "scripts" / "08_export_trial_data.py"
    spec = importlib.util.spec_from_file_location("export_trial_data", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


export_mod = _load_export_module()


def _series(source: str, destination: str, namespace: str = "shared-eval", count: int = 10) -> TrafficSeries:
    return TrafficSeries(
        source_namespace=namespace,
        source_canonical_service=source,
        source_service_account=source,
        source_workload_namespace=namespace,
        destination_namespace=namespace,
        destination_canonical_service=destination,
        destination_service_account=destination,
        destination_service_ref=None,
        protocol="HTTP",
        port=80,
        path="/",
        count=count,
        distinct_days=3,
        last_seen_days_ago=0.0,
    )


def _config(source: str, destination: str, namespace: str = "shared-eval") -> ConfigEvidenceRecord:
    return ConfigEvidenceRecord(
        source_namespace=namespace,
        source_canonical_service=source,
        source_service_account=source,
        destination_namespace=namespace,
        destination_canonical_service=destination,
        destination_service_account=destination,
        protocol=None,
        port=None,
        port_unknown=True,
        config_finding=ConfigFinding.FOUND_STRUCTURAL,
        evidence_source="env",
        scope=Scope.IN_CLUSTER,
        snapshot_id="test-snapshot",
        collected_at="2026-09-27T00:00:00Z",
    )


def _write_ground_truth(dir_path: Path, trial_id: str, pairs: dict) -> None:
    payload = {"|".join(k): v for k, v in pairs.items()}
    (dir_path / f"{trial_id}.json").write_text(json.dumps(payload))


def test_split_keeps_real_edge_for_every_virtual_trial(tmp_path):
    """Cạnh REAL của Online Boutique (frontend->cartservice) phải xuất hiện trong
    TrialInput của MỌI virtual trial, dù không có owner nào trong owner_map — đúng
    nguyên tắc TrialScenario.ground_truth() đã áp dụng cho model namespace-riêng."""
    ns = "shared-eval"
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    _write_ground_truth(gt_dir, "trial-01", {(ns, "frontend", ns, "cartservice"): True})
    _write_ground_truth(gt_dir, "trial-02", {(ns, "frontend", ns, "cartservice"): True})

    real_edge_series = _series("frontend", "cartservice", namespace=ns)

    out_paths = export_mod.split_shared_collection(
        shared_namespace=ns,
        virtual_trial_ids=["trial-01", "trial-02"],
        owner_map={},
        traffic_series=[real_edge_series],
        config_evidence=[],
        scrape_gap=None,
        ground_truth_dir=gt_dir,
        out_dir=tmp_path / "trial_data",
    )
    assert len(out_paths) == 2
    for p in out_paths:
        payload = json.loads(p.read_text())
        assert len(payload["traffic_series"]) == 1
        assert payload["traffic_series"][0]["source_canonical_service"] == "frontend"


def test_split_excludes_edge_owned_only_by_another_trial(tmp_path):
    """Cạnh cấy CHỈ thuộc trial-02 KHÔNG được lẫn vào TrialInput của trial-01 -- nếu lẫn,
    Tier 1 sẽ thấy traffic không nằm trong ground_truth của trial-01, tính nhầm false
    positive (xem NOTE trong split_shared_collection)."""
    ns = "shared-eval"
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    _write_ground_truth(gt_dir, "trial-01", {})
    _write_ground_truth(gt_dir, "trial-02", {(ns, "paymentservice", ns, "currencyservice"): True})

    exclusive_series = _series("paymentservice", "currencyservice", namespace=ns)
    owner_map = {(ns, "paymentservice", ns, "currencyservice"): ["trial-02"]}

    export_mod.split_shared_collection(
        shared_namespace=ns,
        virtual_trial_ids=["trial-01", "trial-02"],
        owner_map=owner_map,
        traffic_series=[exclusive_series],
        config_evidence=[],
        scrape_gap=None,
        ground_truth_dir=gt_dir,
        out_dir=tmp_path / "trial_data",
    )

    trial_01_payload = json.loads((tmp_path / "trial_data" / "trial-01.json").read_text())
    trial_02_payload = json.loads((tmp_path / "trial_data" / "trial-02.json").read_text())
    assert trial_01_payload["traffic_series"] == []
    assert len(trial_02_payload["traffic_series"]) == 1


def test_split_includes_edge_shared_by_multiple_owners_in_all_of_them(tmp_path):
    """Cạnh NOISE_HEALTHCHECK dùng chung (owners=[trial-01, trial-05]) phải xuất hiện
    trong TrialInput của CẢ 2 virtual trial đó — đúng cơ chế SharedEdge.owners."""
    ns = "shared-eval"
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    _write_ground_truth(gt_dir, "trial-01", {(ns, "adservice", ns, "frontend"): False})
    _write_ground_truth(gt_dir, "trial-05", {(ns, "adservice", ns, "frontend"): False})

    shared_series = _series("adservice", "frontend", namespace=ns)
    owner_map = {(ns, "adservice", ns, "frontend"): ["trial-01", "trial-05"]}

    export_mod.split_shared_collection(
        shared_namespace=ns,
        virtual_trial_ids=["trial-01", "trial-05"],
        owner_map=owner_map,
        traffic_series=[shared_series],
        config_evidence=[],
        scrape_gap=None,
        ground_truth_dir=gt_dir,
        out_dir=tmp_path / "trial_data",
    )

    for trial_id in ("trial-01", "trial-05"):
        payload = json.loads((tmp_path / "trial_data" / f"{trial_id}.json").read_text())
        assert len(payload["traffic_series"]) == 1


def test_split_config_evidence_uses_same_ownership_rule_as_traffic(tmp_path):
    ns = "shared-eval"
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    _write_ground_truth(gt_dir, "trial-01", {(ns, "emailservice", ns, "adservice"): True})
    _write_ground_truth(gt_dir, "trial-02", {})

    config_evidence = _config("emailservice", "adservice", namespace=ns)
    owner_map = {(ns, "emailservice", ns, "adservice"): ["trial-01"]}

    export_mod.split_shared_collection(
        shared_namespace=ns,
        virtual_trial_ids=["trial-01", "trial-02"],
        owner_map=owner_map,
        traffic_series=[],
        config_evidence=[config_evidence],
        scrape_gap=None,
        ground_truth_dir=gt_dir,
        out_dir=tmp_path / "trial_data",
    )

    p1 = json.loads((tmp_path / "trial_data" / "trial-01.json").read_text())
    p2 = json.loads((tmp_path / "trial_data" / "trial-02.json").read_text())
    assert len(p1["config_evidence"]) == 1
    assert len(p2["config_evidence"]) == 0


def test_split_skips_trial_with_no_ground_truth_file(tmp_path, capsys):
    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    _write_ground_truth(gt_dir, "trial-01", {})
    # trial-02: KHÔNG tạo file ground truth -- phải bị bỏ qua, không crash.

    out_paths = export_mod.split_shared_collection(
        shared_namespace="shared-eval",
        virtual_trial_ids=["trial-01", "trial-02"],
        owner_map={},
        traffic_series=[],
        config_evidence=[],
        scrape_gap=None,
        ground_truth_dir=gt_dir,
        out_dir=tmp_path / "trial_data",
    )
    assert len(out_paths) == 1
    assert "trial-02" in capsys.readouterr().err
