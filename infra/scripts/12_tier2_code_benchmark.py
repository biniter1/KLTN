#!/usr/bin/env python3
"""Benchmark bằng chứng code cho Tầng 2 (thí nghiệm 1, 04/11/2026).

MỤC ĐÍCH: tách "Tầng 2 kém" khỏi "dataset yếu". Lần chạy 02/11 không phân biệt được hai
nguyên nhân vì mọi cạnh dương được escalate đều KHÔNG có bằng chứng trong code/docs/PR.
Benchmark này đưa cho Tầng 2 các cạnh mà nhãn đúng/sai được quyết định HOÀN TOÀN bởi
bằng chứng các tool đọc được:

  - DƯƠNG (15): 15 cạnh thật của Online Boutique (ONLINE_BOUTIQUE_REAL_EDGES) — code gọi
    thật có trong `src/<source>/` (ngoài file sinh từ proto).
  - ÂM (15): cặp service KHÔNG gọi nhau, rút tất định — source có thể chứa stub sinh từ
    proto của đích (genproto/, *_pb2*.py), tức đúng "bẫy" đã làm Tầng 2 sai ở lần 02/11.

Hai nhóm nhận CÙNG MỘT hồ sơ escalate và CÙNG MỘT kết quả tool traffic (cạnh thưa: 534
request, 1 ngày, lỗi 0%, mọi cửa sổ) — traffic không mang thông tin phân biệt, chỉ code,
docs và PR quyết định. Không có trường config_finding (cũng để không lộ nhãn).

Đây KHÔNG phải số liệu của thí nghiệm 15 trial; ghi rõ trong báo cáo là benchmark riêng.
Chạy CÙNG bộ case trước và sau khi sửa Tầng 2 (--label v2 / v3) để đo đúng phần cải thiện.

Chạy (trên VM, API Server gatekeeper đang chạy, OPENAI_API_KEY + TIER2_PROVIDER/TIER2_MODEL đã đặt):
    python3 12_tier2_code_benchmark.py --db ../../tier2_bench.sqlite3 \\
        --source-root ~/src/microservices-demo --api-server http://localhost:8080 \\
        --label v2 --repeats 3 --out ../tier2_bench_v2.json
    python3 12_tier2_code_benchmark.py --list-cases     # chỉ in bộ case, không gọi LLM
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parent.parent
ROOT = INFRA_DIR.parent
for sub in ("tier1_inference", "tier2_agent", "orchestration"):
    sys.path.insert(0, str(ROOT / sub))
sys.path.insert(0, str(INFRA_DIR / "scenarios"))

from scenario_pool import ONLINE_BOUTIQUE_REAL_EDGES, ONLINE_BOUTIQUE_SERVICES, SERVICE_PORTS  # noqa: E402

NAMESPACE = "shared-eval"
NEG_SEED = 20261104
N_NEGATIVE = 15
# Hồ sơ traffic chung cho MỌI case (cạnh thưa, thành công) — không phân biệt dương/âm.
TRAFFIC_PROFILE = {"count": 534, "distinct_days": 1, "error_ratio": 0.0, "protocol_diversity": 1}
GENERATED_PATH = re.compile(r"(genproto/|_pb2(_grpc)?\.py|\.pb\.go|demo_pb|/protos?/|\.proto\b)")
_NO_SOURCE = {"redis-cart"}  # không có src/ trong Online Boutique


def build_cases() -> list[dict]:
    real = set(ONLINE_BOUTIQUE_REAL_EDGES)
    cases = [{"case_id": f"pos-{i:02d}", "source": s, "destination": d, "label": True}
             for i, (s, d) in enumerate(sorted(real), start=1)]
    pool = sorted((s, d) for s in ONLINE_BOUTIQUE_SERVICES for d in ONLINE_BOUTIQUE_SERVICES
                  if s != d and (s, d) not in real and s not in _NO_SOURCE)
    rng = random.Random(NEG_SEED)
    for i, (s, d) in enumerate(sorted(rng.sample(pool, N_NEGATIVE)), start=1):
        cases.append({"case_id": f"neg-{i:02d}", "source": s, "destination": d, "label": False})
    return cases


def escalation_for(case: dict) -> dict:
    port, protocol = SERVICE_PORTS[case["destination"]]
    return {
        "source_namespace": NAMESPACE,
        "source_canonical_service": case["source"],
        "source_service_account": case["source"],
        "destination_namespace": NAMESPACE,
        "destination_canonical_service": case["destination"],
        "protocol": protocol,
        "port": port,
        "destination_service_ref": {"service_name": case["destination"], "service_namespace": NAMESPACE},
        "scope": "IN_CLUSTER",
        "traffic_class": "BUSINESS",
        "count": TRAFFIC_PROFILE["count"],
        "distinct_days": TRAFFIC_PROFILE["distinct_days"],
        "error_ratio": TRAFFIC_PROFILE["error_ratio"],
        "unstable": False,
        "config_finding": None,
        "score": 0.63,
        "dependency_status": "OBSERVED_WEAK",
        "snapshot_id": "tier2-code-benchmark",
        "workload_revision": None,
    }


class FixedTrafficProvider:
    """Mọi cạnh, mọi cửa sổ -> cùng TRAFFIC_PROFILE (traffic không phân biệt nhãn)."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def fetch_extended_window(self, edge: str, window_days: int) -> dict:
        self.calls.append((edge, window_days))
        return dict(TRAFFIC_PROFILE)


def _support_only_generated(rows) -> bool:
    """True nếu MỌI bằng chứng read_source_code được gán 'support' chỉ trỏ vào file sinh
    từ proto (chẩn đoán lỗi 'nhầm stub là lời gọi thật')."""
    sup = [r for r in rows if r.tool_name == "read_source_code" and r.stance == "support" and r.tool_output]
    if not sup:
        return False
    for r in sup:
        paths = [ln.split(":", 1)[0] for ln in r.tool_output.splitlines() if ":" in ln]
        if any(p and not GENERATED_PATH.search(p) for p in paths):
            return False
    return True


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def _is_out_of_credit(error: str) -> bool:
    e = error.lower()
    return "insufficient_quota" in e or "no credits remaining" in e or "credit balance is too low" in e


def summarize(results: list[dict]) -> dict:
    def metrics(rows, positive):
        tp = sum(1 for r in rows if r["label"] and r["final_status"] in positive)
        fp = sum(1 for r in rows if not r["label"] and r["final_status"] in positive)
        fn = sum(1 for r in rows if r["label"] and r["final_status"] not in positive)
        tn = sum(1 for r in rows if not r["label"] and r["final_status"] not in positive)
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec}

    ok = [r for r in results if r.get("error") is None]
    by_case: dict[str, set] = {}
    for r in ok:
        by_case.setdefault(r["case_id"], set()).add(r["final_status"])
    return {
        "runs": len(results),
        "runs_failed": len(results) - len(ok),
        "confident_only": metrics(ok, {"CONFIDENT"}),
        "confident_or_monitoring": metrics(ok, {"CONFIDENT", "NEEDS_MONITORING"}),
        "original_confident": metrics([{**r, "final_status": r["original_status"]} for r in ok], {"CONFIDENT"}),
        "status_counts_pos": dict(Counter(r["final_status"] for r in ok if r["label"])),
        "status_counts_neg": dict(Counter(r["final_status"] for r in ok if not r["label"])),
        "support_only_generated_code": sum(1 for r in ok if r["support_only_generated"]),
        "cases_inconsistent_across_repeats": sorted(c for c, s in by_case.items() if len(s) > 1),
        "llm_calls": sum(r["llm_calls"] for r in ok),
        "input_tokens": sum(r["input_tokens"] for r in ok),
        "output_tokens": sum(r["output_tokens"] for r in ok),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list-cases", action="store_true")
    p.add_argument("--db", default=str(ROOT / "tier2_bench.sqlite3"))
    p.add_argument("--source-root")
    p.add_argument("--api-server", default="http://localhost:8080")
    p.add_argument("--label", required=False, default="run", help="nhãn phiên bản Tầng 2, vd v2 / v3")
    p.add_argument("--repeats", type=int, default=3, help="số lần chạy mỗi case (đo độ ổn định)")
    p.add_argument("--only", nargs="*", help="chỉ chạy các case_id này")
    p.add_argument("--out")
    args = p.parse_args(argv)

    cases = build_cases()
    if args.only:
        cases = [c for c in cases if c["case_id"] in set(args.only)]
    if args.list_cases:
        for c in cases:
            print(f'{c["case_id"]}  {"DƯƠNG" if c["label"] else "ÂM   "}  {c["source"]} -> {c["destination"]}')
        return 0
    if not args.source_root or not args.out:
        p.error("cần --source-root và --out (hoặc --list-cases)")

    from orchestration.wiring import build_brain_runner
    from tier2_agent.storage import Storage

    # Chặn chạy lại cùng --label trên cùng DB TRƯỚC khi gọi LLM (05/11: lần chạy v3 lỗi
    # "UNIQUE constraint failed: agent_sessions.run_id" vì mã phiên đã có từ lần chạy trước).
    if Path(args.db).exists():
        import sqlite3

        with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as ro:
            try:
                n_old = ro.execute("SELECT count(*) FROM agent_sessions WHERE run_id LIKE ?",
                                   (f"bench-{args.label}-%",)).fetchone()[0]
            except sqlite3.OperationalError:
                n_old = 0
        if n_old:
            print(f"LỖI: DB đã có {n_old} phiên với nhãn '{args.label}'. Dùng --label khác "
                  f"(vd {args.label}b) hoặc --db khác để không trùng mã phiên.", file=sys.stderr)
            return 2

    traffic = FixedTrafficProvider()
    runner = build_brain_runner(db_path=Path(args.db), api_server_url=args.api_server,
                                source_root=Path(args.source_root), prometheus_client=None,
                                namespaces=[NAMESPACE], traffic_provider=traffic)
    storage = Storage(args.db)
    results = []
    for rep in range(1, args.repeats + 1):
        for c in cases:
            sid = f"bench-{args.label}-r{rep}-{c['case_id']}"
            row = {**c, "repeat": rep, "session_id": sid, "error": None}
            try:
                res = runner.run(escalation_for(c), sid)
                usage = storage.get_llm_usage(sid)
                trace = storage.get_trace_rows(sid)
                row.update({
                    "final_status": res.final_status, "original_status": res.original_agent_status,
                    "rounds": res.iteration, "stopped_by": res.stopped_by, "stop_reason": res.stop_reason,
                    "last_reasoning": (res.reasoning or "")[:600],
                    "tools_used": dict(Counter(t.tool_name for t in trace)),
                    "support_only_generated": _support_only_generated(trace),
                    "llm_calls": len(usage),
                    "input_tokens": sum(u["input_tokens"] for u in usage),
                    "output_tokens": sum(u["output_tokens"] for u in usage),
                })
            except Exception as exc:  # noqa: BLE001 — ghi lại, chạy tiếp case khác
                row.update({"error": f"{type(exc).__name__}: {exc}", "final_status": None,
                            "original_status": None, "support_only_generated": False,
                            "llm_calls": 0, "input_tokens": 0, "output_tokens": 0})
            results.append(row)
            print(f'[r{rep}] {c["case_id"]} {c["source"]}->{c["destination"]} '
                  f'{"DƯƠNG" if c["label"] else "ÂM"}: {row["final_status"] or row["error"]}', flush=True)
            if row["error"] and _is_out_of_credit(row["error"]):
                print("DỪNG: nhà cung cấp LLM báo hết credit/quota — các case còn lại không chạy. "
                      "Kết quả đã chạy vẫn được ghi ra --out.", file=sys.stderr, flush=True)
                break
        else:
            continue
        break

    import os
    report = {
        "_meta": {
            "label": args.label, "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": _git_commit(), "tier2_provider": os.environ.get("TIER2_PROVIDER", "anthropic"),
            "tier2_model": os.environ.get("TIER2_MODEL"), "repeats": args.repeats,
            "traffic_profile": TRAFFIC_PROFILE, "negative_seed": NEG_SEED,
            "note": "Benchmark bằng chứng code (thí nghiệm 1) — KHÔNG phải số liệu 15 trial.",
        },
        "summary": summarize(results),
        "results": results,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    s = report["summary"]
    print(json.dumps({k: s[k] for k in ("confident_only", "confident_or_monitoring", "status_counts_pos",
                                        "status_counts_neg", "support_only_generated_code",
                                        "cases_inconsistent_across_repeats")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
