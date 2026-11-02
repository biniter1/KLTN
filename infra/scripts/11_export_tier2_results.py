#!/usr/bin/env python3
"""Xuất kết quả Tầng 2 và tính recall gộp Tầng 1 + Tầng 2 từ SQLite của Batch Orchestrator.

CHỈ ĐỌC: mở SQLite ở chế độ read-only, không ghi bảng nào. Chạy SAU khi
`orchestration.cli_batch` đã chạy xong các trial (cùng file `--db`).

Với mỗi trial lấy lần chạy (attempt) SUCCESS mới nhất, rồi:
  1. Bảng từng cạnh Tầng 1 escalate: trạng thái Tầng 1, `tier2_status`,
     `tier2_original_status`, số vòng suy luận, lý do dừng, token (bảng `llm_usage`),
     nhãn đúng/sai so với ground truth.
  2. Tóm tắt từng trial: số cạnh escalate, phân bố kết luận, tổng vòng, tổng token, lỗi.
  3. Precision/recall/F1 theo trial cho: Tầng 1 riêng, Tầng 1 + Tầng 2, B1, B2;
     mean ± CI 95% và Wilcoxon bắt cặp (cùng hàm `tier1.evaluation` mà 09 dùng).

Quy ước (chốt 02/11/2026):
  - "Dự đoán dương" của Tầng 1 = `OBSERVED_STRONG` (như `tier1.evaluation`).
  - Tầng 1 + Tầng 2 = Tầng 1 dương ∪ cạnh Tầng 2 kết luận `CONFIDENT`
    (`--tier2-positive` đổi được, mặc định chỉ CONFIDENT). NEEDS_MONITORING báo riêng.
  - B1 = mọi cạnh có traffic (count > 0); B2 = count >= 20 và distinct_days >= 3 — tính
    lại từ chính các dòng `dependency_evidence` của attempt (đúng định nghĩa `baseline.py`).
  - Cạnh đích chưa phân giải (`raw:<host>`, kịch bản CONFIG_ONLY_UNRESOLVED) được quy về
    cặp cấy theo quy ước `<đích>-nonexistent` của `trial_scope.py`; nếu không quy được thì
    giữ nguyên (không khớp ground truth nào).

Dùng:
    python3 11_export_tier2_results.py --db ../../kltn_tier2.sqlite3 \\
        --ground-truth-dir ../ground_truth --out-dir ../tier2_results
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

INFRA_DIR = Path(__file__).resolve().parent.parent
UNRESOLVED_TARGET_SUFFIX = "-nonexistent"
PairKey = tuple


def _add_tier1_to_path(tier1_path: str) -> None:
    sys.path.insert(0, str(Path(tier1_path).resolve()))


def open_readonly(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise SystemExit(f"LỖI: không thấy file DB '{db_path}'")
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def load_ground_truth(path: Path) -> dict:
    raw = json.loads(Path(path).read_text())
    return {tuple(k.split("|")): bool(v) for k, v in raw.items()}


def pair_of(row) -> PairKey:
    """pair_key của 1 dòng dependency_evidence; quy `raw:<đích>-nonexistent` về cặp cấy."""
    dst_svc = row["destination_canonical_service"] or ""
    if row["destination_namespace"] is None and dst_svc.startswith("raw:"):
        host = dst_svc[len("raw:"):].split(".")[0]
        if host.endswith(UNRESOLVED_TARGET_SUFFIX):
            target = host[: -len(UNRESOLVED_TARGET_SUFFIX)]
            return (row["source_namespace"], row["source_canonical_service"], row["source_namespace"], target)
    return (row["source_namespace"], row["source_canonical_service"], row["destination_namespace"], dst_svc)


def latest_success_attempt(conn, run_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT attempt_id FROM pipeline_run_log WHERE run_id = ? AND step = 'trial' AND status = 'SUCCESS' "
        "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    return row["attempt_id"] if row else None


def attempt_errors(conn, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT attempt_id, step, error_message, timestamp FROM pipeline_run_log "
        "WHERE run_id = ? AND status = 'FAILED' ORDER BY id", (run_id,)).fetchall()
    return [dict(r) for r in rows]


def metrics(predicted: set, ground_truth: dict) -> dict:
    """Đúng công thức `tier1.evaluation.precision_recall_f1` (zero_division = 0)."""
    actual = {pk for pk, real in ground_truth.items() if real}
    tp, fp, fn = len(predicted & actual), len(predicted - actual), len(actual - predicted)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": p, "recall": r, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def session_info(conn, session_id: Optional[str]) -> dict:
    out = {"rounds": None, "stopped_by": None, "stop_reason": None, "last_reasoning": None,
           "llm_calls": None, "input_tokens": None, "output_tokens": None,
           "cache_creation_input_tokens": None, "cache_read_input_tokens": None, "model": None}
    if not session_id:
        return out
    if _has_table(conn, "agent_rounds"):
        rounds = conn.execute("SELECT iteration, stopped_by, stop_reason, reasoning FROM agent_rounds "
                              "WHERE run_id = ? ORDER BY iteration", (session_id,)).fetchall()
        out["rounds"] = len(rounds)
        if rounds:
            last = rounds[-1]
            out["stopped_by"], out["stop_reason"] = last["stopped_by"], last["stop_reason"]
            out["last_reasoning"] = (last["reasoning"] or "")[:300]
    if _has_table(conn, "llm_usage"):
        u = conn.execute(
            "SELECT COUNT(*) AS n, SUM(input_tokens) AS i, SUM(output_tokens) AS o, "
            "SUM(cache_creation_input_tokens) AS cc, SUM(cache_read_input_tokens) AS cr, MAX(model) AS m "
            "FROM llm_usage WHERE run_id = ?", (session_id,)).fetchone()
        out.update(llm_calls=u["n"], input_tokens=u["i"] or 0, output_tokens=u["o"] or 0,
                   cache_creation_input_tokens=u["cc"] or 0, cache_read_input_tokens=u["cr"] or 0, model=u["m"])
    return out


def analyse_trial(conn, run_id: str, ground_truth: dict, tier2_positive: set) -> dict:
    attempt = latest_success_attempt(conn, run_id)
    errors = attempt_errors(conn, run_id)
    if attempt is None:
        return {"run_id": run_id, "attempt_id": None, "status": "NO_SUCCESS_ATTEMPT", "errors": errors}

    rows = conn.execute("SELECT * FROM dependency_evidence WHERE run_id = ? AND attempt_id = ?",
                        (run_id, attempt)).fetchall()
    actual = {pk for pk, real in ground_truth.items() if real}

    tier1_pos = {pair_of(r) for r in rows if r["dependency_status"] == "OBSERVED_STRONG"}
    tier2_pos = {pair_of(r) for r in rows if r["escalate_to_tier2"] and r["tier2_status"] in tier2_positive}
    b1_pos = {pair_of(r) for r in rows if (r["count"] or 0) > 0}
    b2_pos = {pair_of(r) for r in rows if (r["count"] or 0) >= 20 and (r["distinct_days"] or 0) >= 3}

    edges = []
    for r in rows:
        if not r["escalate_to_tier2"]:
            continue
        pk = pair_of(r)
        gt = ground_truth.get(pk)
        positive = r["tier2_status"] in tier2_positive
        if r["tier2_status"] is None:
            verdict = "NO_TIER2_RESULT"
        elif positive:
            verdict = "TP" if pk in actual else "FP"
        else:
            verdict = "FN" if pk in actual else "TN"
        edges.append({
            "run_id": run_id, "attempt_id": attempt, "identity_key": r["identity_key"],
            "pair_key": "|".join(str(x) for x in pk),
            "protocol": r["protocol"], "port": r["port"], "traffic_class": r["traffic_class"],
            "count": r["count"], "distinct_days": r["distinct_days"], "score": r["score"],
            "tier1_status": r["dependency_status"], "escalation_reason": r["escalation_reason"],
            "tier2_status": r["tier2_status"], "tier2_original_status": r["tier2_original_status"],
            "tier2_session_id": r["tier2_session_id"],
            "ground_truth": gt, "tier2_verdict": verdict,
            **session_info(conn, r["tier2_session_id"]),
        })

    status_counts = Counter(e["tier2_status"] or "NONE" for e in edges)
    sum_or_none = lambda key: (sum(e[key] or 0 for e in edges) if any(e[key] is not None for e in edges) else None)
    return {
        "run_id": run_id, "attempt_id": attempt, "status": "OK", "errors": errors,
        "escalated": len(edges), "tier2_status_counts": dict(status_counts),
        "rounds_total": sum_or_none("rounds"), "llm_calls_total": sum_or_none("llm_calls"),
        "input_tokens_total": sum_or_none("input_tokens"), "output_tokens_total": sum_or_none("output_tokens"),
        "metrics": {
            "tier1": metrics(tier1_pos, ground_truth),
            "tier1_plus_tier2": metrics(tier1_pos | tier2_pos, ground_truth),
            "b1": metrics(b1_pos, ground_truth),
            "b2": metrics(b2_pos, ground_truth),
        },
        "edges": edges,
    }


def summarise(trials: list[dict]) -> dict:
    from tier1.evaluation import mean_ci95, wilcoxon_signed_rank

    ok = [t for t in trials if t["status"] == "OK"]
    if not ok:
        return {"n_trials": 0}

    def series(variant, metric):
        return [t["metrics"][variant][metric] for t in ok]

    summary = {"n_trials": len(ok)}
    for v in ("tier1", "tier1_plus_tier2", "b1", "b2"):
        summary[v] = {f"{m}_ci": dataclasses.asdict(mean_ci95(series(v, m))) for m in ("precision", "recall", "f1")}
    for a, b in (("tier1_plus_tier2", "tier1"), ("tier1_plus_tier2", "b1"), ("tier1_plus_tier2", "b2"),
                 ("tier1", "b1"), ("tier1", "b2")):
        for m in ("recall", "precision"):
            summary[f"{a}_vs_{b}_{m}"] = dataclasses.asdict(wilcoxon_signed_rank(series(a, m), series(b, m)))
    return summary


def git_head(repo_dir: Path) -> Optional[str]:
    try:
        return subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="SQLite đã dùng cho cli_batch")
    p.add_argument("--ground-truth-dir", default=str(INFRA_DIR / "ground_truth"))
    p.add_argument("--run-ids", nargs="*", default=None, help="mặc định: trial-01..trial-15")
    p.add_argument("--tier2-positive", nargs="+", default=["CONFIDENT"])
    p.add_argument("--tier1-path", default=str(INFRA_DIR.parent / "tier1_inference"))
    p.add_argument("--out-dir", default=str(INFRA_DIR / "tier2_results"))
    args = p.parse_args(argv)
    _add_tier1_to_path(args.tier1_path)

    run_ids = args.run_ids or [f"trial-{i:02d}" for i in range(1, 16)]
    conn = open_readonly(Path(args.db))
    trials = []
    for run_id in run_ids:
        gt_path = Path(args.ground_truth_dir) / f"{run_id}.json"
        if not gt_path.exists():
            trials.append({"run_id": run_id, "status": "NO_GROUND_TRUTH", "errors": []})
            continue
        trials.append(analyse_trial(conn, run_id, load_ground_truth(gt_path), set(args.tier2_positive)))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": git_head(INFRA_DIR.parent),
            "db": str(Path(args.db).resolve()),
            "tier2_positive": args.tier2_positive,
            "note": "Số đo từ lần chạy thật của Batch Orchestrator; script chỉ đọc SQLite.",
        },
        "summary": summarise(trials),
        "trials": [{k: v for k, v in t.items() if k != "edges"} for t in trials],
    }
    (out_dir / "tier2_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    edges = [e for t in trials for e in t.get("edges", [])]
    (out_dir / "tier2_edges.json").write_text(json.dumps(edges, ensure_ascii=False, indent=2))
    if edges:
        with open(out_dir / "tier2_edges.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(edges[0].keys()))
            w.writeheader()
            w.writerows(edges)
    wrong = [e for e in edges if e["tier2_verdict"] in ("FP", "FN")]
    (out_dir / "tier2_wrong.json").write_text(json.dumps(wrong, ensure_ascii=False, indent=2))

    s = report["summary"]
    print(f"Trial có attempt SUCCESS: {s.get('n_trials', 0)}/{len(run_ids)}")
    for t in trials:
        if t["status"] != "OK":
            print(f"  {t['run_id']}: {t['status']}  lỗi: {t.get('errors')}")
    if s.get("n_trials"):
        for v in ("tier1", "tier1_plus_tier2", "b1", "b2"):
            r = s[v]["recall_ci"]
            pr = s[v]["precision_ci"]
            print(f"  {v:18s} recall={r['mean']:.3f}±{r['half_width']:.3f}  precision={pr['mean']:.3f}±{pr['half_width']:.3f}")
    print(f"Cạnh escalate: {len(edges)} · kết luận sai (FP/FN): {len(wrong)} · ghi vào {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
