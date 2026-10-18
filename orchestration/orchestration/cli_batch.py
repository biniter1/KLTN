"""CLI Batch Orchestrator — chạy ĐÚNG 1 trial rồi thoát (exit 0 = SUCCESS, 1 = FAILED).

Ví dụ (hạ tầng dùng chung, sau khi đã đủ 7 ngày quan sát):

    kubectl -n istio-system port-forward svc/prometheus 9090:9090 &
    export OPENAI_API_KEY="..."          # hoặc provider khác của Config Collector
    export ANTHROPIC_API_KEY="..."       # Tier 2 (bỏ nếu --skip-tier2)
    python -m orchestration.cli_batch --run-id trial-07 \\
        --namespace shared-eval \\
        --owner-map ../infra/.cache/shared-eval.owner_map.json \\
        --db ../kltn.sqlite3 \\
        --source-root ~/src/microservices-demo \\
        --api-server http://localhost:8080

Chạy cả 15 trial = gọi lệnh trên 15 lần (vòng for trong shell) — mỗi lần 1 tiến trình mới.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import _paths  # noqa: F401
from .batch import BatchDeps, BatchOrchestrator, BatchSettings

_INFRA = Path(__file__).resolve().parent.parent.parent / "infra"


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", required=True, help="virtual trial id, vd trial-07")
    p.add_argument("--namespace", default="shared-eval", help="namespace K8s dùng chung (05_deploy_shared.sh)")
    p.add_argument("--db", default="kltn.sqlite3", help="file SQLite DÙNG CHUNG với API Server")
    p.add_argument("--ground-truth-dir", default=str(_INFRA / "ground_truth"))
    p.add_argument("--owner-map", default=None, help="mặc định: infra/.cache/<namespace>.owner_map.json")
    p.add_argument("--window-days", type=int, default=7)
    p.add_argument("--prometheus-url", default="http://localhost:9090")
    p.add_argument("--kubeconfig", default=None)
    p.add_argument("--deployed-at", default=None,
                   help="ISO time lúc harness bắt đầu chạy; mặc định tự đọc startTime của pod test-harness=true")
    p.add_argument("--allow-short-window", action="store_true",
                   help="CHỈ để thử pipeline: bỏ qua kiểm tra đã đủ --window-days")
    p.add_argument("--fail-on-scrape-gap", action="store_true",
                   help="hủy trial nếu Prometheus báo scrape gap (mặc định chỉ ghi cờ vào collection_quality_log)")
    p.add_argument("--stateful", action="store_true",
                   help="Tier 1 dùng hysteresis theo lịch sử CHÍNH trial này (bộ dữ liệu demo); "
                        "mặc định stateless như tier1.experiment.run_trial (R=15)")
    p.add_argument("--skip-tier2", action="store_true", help="không gọi Tier 2 (đo Tier 1 thuần)")
    p.add_argument("--source-root", default=None, help="[Tier 2] bản checkout microservices-demo")
    p.add_argument("--api-server", default="http://localhost:8080", help="[Tier 2] gatekeeper /agent-tool-calls/*")
    p.add_argument("--image-config", action="store_true",
                   help="đọc ENV/ENTRYPOINT/CMD của image qua crane (CraneImageConfigReader)")
    return p


def main(argv=None) -> int:  # pragma: no cover - nối hạ tầng thật; logic đã test qua BatchOrchestrator
    args = build_parser().parse_args(argv)

    from tier1.config_collector import CraneImageConfigReader, KubernetesReader
    from tier1.traffic_collector import HttpPrometheusClient

    from .wiring import build_brain_runner, fixed_deployment_clock, kubernetes_deployment_clock

    owner_map = Path(args.owner_map) if args.owner_map else _INFRA / ".cache" / f"{args.namespace}.owner_map.json"
    prom = HttpPrometheusClient(base_url=args.prometheus_url)
    clock = (fixed_deployment_clock(_parse_dt(args.deployed_at)) if args.deployed_at
             else kubernetes_deployment_clock(args.kubeconfig))

    tier2_runner = None
    if not args.skip_tier2:
        if not args.source_root:
            print("LỖI: cần --source-root (bản checkout microservices-demo) cho Tier 2, hoặc --skip-tier2",
                  file=sys.stderr)
            return 2
        tier2_runner = build_brain_runner(db_path=Path(args.db), api_server_url=args.api_server,
                                          source_root=Path(args.source_root), prometheus_client=prom,
                                          namespaces=[args.namespace])

    settings = BatchSettings(
        run_id=args.run_id, db_path=Path(args.db), namespace=args.namespace,
        ground_truth_dir=Path(args.ground_truth_dir), owner_map_path=owner_map,
        window_days=args.window_days, allow_short_window=args.allow_short_window,
        fail_on_scrape_gap=args.fail_on_scrape_gap, stateful_tier1=args.stateful,
    )
    deps = BatchDeps(
        prometheus_client=prom,
        k8s_reader=KubernetesReader(kubeconfig_path=args.kubeconfig),
        image_reader=CraneImageConfigReader() if args.image_config else None,
        deployment_clock=clock,
        tier2_runner=tier2_runner,
    )
    outcome = BatchOrchestrator(settings, deps).run()
    if outcome.status == "SUCCESS":
        print(f"[{outcome.attempt_id}] SUCCESS — Tier 1: {outcome.tier1_status_counts}; "
              f"Tier 2: {outcome.tier2_status_counts or '(bỏ qua / không có cạnh escalate)'}")
        return 0
    print(f"[{outcome.attempt_id}] FAILED ở bước {outcome.failed_step}: {outcome.error_message}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
