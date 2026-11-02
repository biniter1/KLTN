"""11_export_tier2_results.py — chỉ đọc SQLite của Batch Orchestrator, tính recall gộp
Tầng 1 + Tầng 2. Dữ liệu ở đây là dòng giả lập dựng tay để KIỂM CÔNG THỨC của script,
không phải kết quả Tầng 2."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parent.parent
ROOT = INFRA_DIR.parent
for sub in ("tier1_inference", "tier2_agent", "orchestration"):
    sys.path.insert(0, str(ROOT / sub))

NS = "shared-eval"


def _load():
    path = INFRA_DIR / "scripts" / "11_export_tier2_results.py"
    spec = importlib.util.spec_from_file_location("export_tier2_results", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_db(path: Path):
    from orchestration.db import open_db
    from tier1 import storage as tier1_storage
    from tier2_agent.storage import Storage

    tier1_storage.connect(path).close()
    open_db(path).close()
    t2 = Storage(str(path))
    conn = sqlite3.connect(path)

    def log(run_id, attempt, step, status, err=None):
        conn.execute("INSERT INTO pipeline_run_log (run_id, attempt_id, step, status, error_message, timestamp) "
                     "VALUES (?, ?, ?, ?, ?, '2026-11-02T00:00:00')", (run_id, attempt, step, status, err))

    def edge(attempt, src, dst, status, *, count=100, days=7, esc=0, t2s=None, sid=None, dst_ns=NS):
        conn.execute(
            "INSERT INTO dependency_evidence (source_namespace, source_canonical_service, source_service_account, "
            "destination_namespace, destination_canonical_service, protocol, port, scope, traffic_class, count, "
            "distinct_days, error_ratio, dependency_status, snapshot_id, created_at, run_id, attempt_id, "
            "escalate_to_tier2, tier2_status, tier2_original_status, tier2_session_id, identity_key) "
            "VALUES (?, ?, ?, ?, ?, 'GRPC', 7070, 'IN_CLUSTER', 'BUSINESS', ?, ?, 0, ?, ?, 'now', 'trial-01', ?, "
            "?, ?, ?, ?, ?)",
            (NS, src, src, dst_ns, dst, count, days, status, attempt, attempt, esc, t2s, t2s, sid, f"{src}->{dst}"))

    # attempt a1 FAILED ở tier2, attempt a2 SUCCESS -> chỉ lấy a2
    log("trial-01", "trial-01.a1", "tier2", "FAILED", "orchestrator_hook cập nhật 0 dòng")
    log("trial-01", "trial-01.a1", "trial", "FAILED", "bước tier2 lỗi")
    edge("trial-01.a1", "a", "z", "OBSERVED_STRONG")
    log("trial-01", "trial-01.a2", "trial", "SUCCESS")
    edge("trial-01.a2", "a", "b", "OBSERVED_STRONG")                                  # TP Tầng 1
    edge("trial-01.a2", "a", "c", "OBSERVED_WEAK", count=3, days=1, esc=1, t2s="CONFIDENT", sid="s1")  # Tầng 2 cứu
    edge("trial-01.a2", "a", "d", "OBSERVED_WEAK", count=2, days=1, esc=1, t2s="INSUFFICIENT_BASIS", sid="s2")
    edge("trial-01.a2", "x", "y", "OBSERVED_WEAK", count=50, days=7, esc=1, t2s="CONFIDENT", sid="s3")  # FP Tầng 2
    edge("trial-01.a2", "a", "raw:e-nonexistent", "NOT_OBSERVED", count=0, days=0, esc=1, t2s="NEEDS_MONITORING",
         sid="s4", dst_ns=None)
    conn.commit()
    conn.close()
    t2._conn.execute("INSERT INTO agent_rounds (run_id, iteration, reasoning, stopped_by, stop_reason, timestamp) "
                     "VALUES ('s2', 1, 'r1', NULL, NULL, 0), ('s2', 2, 'không thấy bằng chứng', 'rule', "
                     "'ALL_INSUFFICIENT', 0)")
    t2._conn.commit()
    t2.append_llm_usage("s2", 1, {"model": "m", "input_tokens": 1000, "output_tokens": 100})
    t2.append_llm_usage("s2", 2, {"model": "m", "input_tokens": 1500, "output_tokens": 120})
    t2.close()


def test_export_metrics_and_edges(tmp_path):
    mod = _load()
    db = tmp_path / "k.sqlite3"
    _build_db(db)
    gt_dir = tmp_path / "gt"
    gt_dir.mkdir()
    (gt_dir / "trial-01.json").write_text(json.dumps({
        f"{NS}|a|{NS}|b": True, f"{NS}|a|{NS}|c": True, f"{NS}|a|{NS}|d": True,
        f"{NS}|a|{NS}|e": True, f"{NS}|x|{NS}|y": False,
    }))
    out = tmp_path / "out"
    assert mod.main(["--db", str(db), "--ground-truth-dir", str(gt_dir), "--run-ids", "trial-01",
                     "--out-dir", str(out)]) == 0

    report = json.loads((out / "tier2_report.json").read_text())
    t = report["trials"][0]
    assert t["attempt_id"] == "trial-01.a2" and len(t["errors"]) == 2
    m = t["metrics"]
    assert (m["tier1"]["tp"], m["tier1"]["fn"]) == (1, 3)
    assert (m["tier1_plus_tier2"]["tp"], m["tier1_plus_tier2"]["fp"], m["tier1_plus_tier2"]["fn"]) == (2, 1, 2)
    assert m["b1"]["fp"] == 1  # x->y có traffic
    assert t["escalated"] == 4 and t["tier2_status_counts"]["CONFIDENT"] == 2

    edges = {e["pair_key"]: e for e in json.loads((out / "tier2_edges.json").read_text())}
    assert edges[f"{NS}|a|{NS}|e"]["tier2_verdict"] == "FN"  # raw:e-nonexistent quy về cặp cấy
    d = edges[f"{NS}|a|{NS}|d"]
    assert d["rounds"] == 2 and d["stop_reason"] == "ALL_INSUFFICIENT"
    assert d["input_tokens"] == 2500 and d["llm_calls"] == 2
    wrong = json.loads((out / "tier2_wrong.json").read_text())
    assert {w["tier2_verdict"] for w in wrong} == {"FP", "FN"} and len(wrong) == 3


def test_db_opened_read_only(tmp_path):
    mod = _load()
    db = tmp_path / "k.sqlite3"
    _build_db(db)
    conn = mod.open_readonly(db)
    try:
        conn.execute("DELETE FROM dependency_evidence")
        raised = False
    except sqlite3.OperationalError:
        raised = True
    assert raised
