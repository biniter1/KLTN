#!/usr/bin/env python3
"""Nối `KubernetesReader`/`HttpPrometheusClient` (đã code, đã test — tier1/config_collector.py,
tier1/traffic_collector.py) với 1 trial THẬT đã deploy, xuất ra 1 file JSON/trial chứa đủ dữ
liệu cho `tier1.experiment.TrialInput` (traffic_series + config_evidence + ground_truth).

CHƯA kiểm chứng trên cluster thật (không có cluster trong sandbox soạn script này) — logic
gọi `collect_config_evidence()`/`collect_traffic()` đã có 181 test trên dữ liệu giả lập,
nhưng bản thân việc kết nối tới kubeconfig/Prometheus thật ở đây thì chưa.

Yêu cầu trước khi chạy:
  - `kubectl config current-context` trỏ đúng cluster (kind: kltn-eval, xem 02_create_cluster.sh)
  - Prometheus lộ ra tại --prometheus-url (thường qua port-forward):
        kubectl -n istio-system port-forward svc/prometheus 9090:9090 &
  - Đã deploy trial cần export (05_deploy_trial.sh) VÀ đã đợi đủ window-days ngày quan sát
    thật (mặc định 7 — window_days trong tier1/config.py) kể từ lúc harness pod đầu tiên
    Running, KHÔNG phải từ lúc namespace được tạo.
  - `pip install kubernetes requests` (2 thư viện thật của 2 client, lazy-import nên chỉ cần
    khi thật sự chạy, không cần khi chạy unit test).

Cập nhật 15/10/2026: nguồn metrics chọn được Prometheus hoặc VictoriaMetrics (--metrics-backend,
qua vmauth của repo LOG_METRIC), tùy chọn đọc access log ztunnel từ VictoriaLogs (--logs-url),
đọc cluster context 1 lần (suy port từ Service, path probe, danh tính monitoring, ServiceEntry —
gắn thẳng vào từng series nên 09_run_experiment chạy offline vẫn đúng), và ghi thêm
`series_daily_counts` (số request theo ngày của từng series, cùng thứ tự `traffic_series`) cho
ablation hysteresis (10_hysteresis_ablation.py). Chạy lại script này trên snapshot Prometheus đã
chụp là đủ — KHÔNG cần thu lại 7 ngày.

Dùng:
    python3 08_export_trial_data.py trial-01 trial-02 ... \\
        --prometheus-url http://localhost:9090 \\
        --tier1-path ../../tier1_inference \\
        --ground-truth-dir ../ground_truth \\
        --out-dir ../trial_data
    # hoặc --all để tự tìm mọi trial_id đã có file ground-truth (đã deploy qua 05/06_*.sh)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path


def _add_tier1_to_path(tier1_path: str) -> None:
    p = Path(tier1_path).resolve()
    if not p.exists():
        raise SystemExit(
            f"LỖI: không tìm thấy thư mục tier1_inference tại '{p}'. "
            f"Truyền đúng đường dẫn qua --tier1-path (thư mục CHỨA package 'tier1/', "
            f"tức thư mục có file tier1/__init__.py bên trong)."
        )
    sys.path.insert(0, str(p))


def _load_ground_truth(path: Path) -> dict[tuple, bool]:
    raw = json.loads(path.read_text())
    gt = {}
    for key_str, value in raw.items():
        parts = tuple(key_str.split("|"))
        if len(parts) != 4:
            raise ValueError(f"Ground truth key không đúng định dạng 'ns|src|ns|dst': {key_str!r}")
        gt[parts] = bool(value)
    return gt


def _load_owner_map(path: Path) -> dict[tuple, list[str]]:
    """Đọc owner_map.json do `render_manifests.py --shared --dump-owner-map` sinh —
    pair_key ('ns|src|ns|dst') -> danh sách virtual_trial_id sở hữu cạnh đó. Dùng ở
    `export_shared_trials` để tách 1 lần thu thập traffic/config CHUNG thành N
    `TrialInput` riêng, xem `scenario_pool.SharedInfraScenario.owner_map`."""
    raw = json.loads(path.read_text())
    om: dict[tuple, list[str]] = {}
    for key_str, owners in raw.items():
        parts = tuple(key_str.split("|"))
        if len(parts) != 4:
            raise ValueError(f"Owner map key không đúng định dạng 'ns|src|ns|dst': {key_str!r}")
        om[parts] = list(owners)
    return om


def _to_jsonable(obj):
    """dataclasses.asdict() không tự xử lý Enum -> value; tự viết default handler đơn
    giản thay vì kéo thêm thư viện ngoài chỉ cho việc này."""
    if dataclasses.is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "value") and type(obj).__mro__[1].__name__ == "Enum":  # Enum con
        return obj.value
    return obj


def _make_clients(prometheus_url: str, metrics_backend: str | None, logs_url: str | None, allow_partial: bool):
    from tier1.log_collector import HttpVictoriaLogsClient
    from tier1.traffic_collector import make_metrics_client

    metrics = make_metrics_client(prometheus_url, metrics_backend, allow_partial=allow_partial)
    logs = HttpVictoriaLogsClient(logs_url) if logs_url else None
    return metrics, logs


def _collect_traffic_with_context(k8s_reader, prometheus_url, metrics_backend, logs_url, allow_partial,
                                  window_days, namespaces, monitoring_services):
    from tier1.cluster_context import build_cluster_context
    from tier1.traffic_collector import collect_traffic

    metrics, logs = _make_clients(prometheus_url, metrics_backend, logs_url, allow_partial)
    context = build_cluster_context(k8s_reader, monitoring_services)
    return collect_traffic(metrics, window_days=window_days, namespaces=namespaces, injected=None,
                           context=context, log_client=logs)


def _port_source_summary(series) -> dict:
    out: dict = {}
    for s in series:
        out[s.port_source] = out.get(s.port_source, 0) + 1
    return out


def export_one_trial(
    trial_id: str,
    prometheus_url: str,
    ground_truth_dir: Path,
    out_dir: Path,
    window_days: int,
    kubeconfig_path: str | None,
    cache_db_path: str | None = None,
    metrics_backend: str | None = None,
    logs_url: str | None = None,
    allow_partial: bool = False,
    monitoring_services: tuple = ("istio-system/prometheus",),
) -> Path:
    from tier1.config_collector import KubernetesReader, collect_config_evidence

    gt_path = ground_truth_dir / f"{trial_id}.json"
    if not gt_path.exists():
        raise SystemExit(
            f"LỖI: không thấy ground truth '{gt_path}' -- trial '{trial_id}' đã deploy qua "
            f"05_deploy_trial.sh/06_deploy_all_trials.sh chưa? (script đó tự ghi file này)"
        )
    ground_truth = _load_ground_truth(gt_path)

    print(f"[{trial_id}] Đọc config evidence từ cluster thật (namespace={trial_id})...")
    k8s_reader = KubernetesReader(kubeconfig_path=kubeconfig_path)
    # cache_db_path -- cache LLM BỀN qua nhiều lần CHẠY SCRIPT khác nhau (không chỉ trong
    # 1 lần gọi hàm, mục 2/4.3): thiếu cái này, mỗi lần chạy lại script (dry-run test lại,
    # hoặc 1 trial lỗi phải export lại) sẽ gọi LLM lại TỪ ĐẦU cho mọi chunk, kể cả chunk đã
    # extract thành công trước đó -- đốt token free tier vô ích (đã gặp thật: Groq free tier
    # cạn token/ngày chỉ sau vài lần dry-run lặp lại, xem lịch sử chat 2026-10-02).
    if cache_db_path:
        Path(cache_db_path).parent.mkdir(parents=True, exist_ok=True)
    config_evidence = collect_config_evidence(
        k8s_reader, snapshot_id=f"{trial_id}-snapshot", namespaces=[trial_id], db_path=cache_db_path,
    )
    print(f"[{trial_id}]   -> {len(config_evidence)} config evidence record")

    print(f"[{trial_id}] Đọc traffic {window_days} ngày ({metrics_backend or 'prometheus'}: {prometheus_url}"
          f"{', logs: ' + logs_url if logs_url else ''})...")
    collection = _collect_traffic_with_context(
        k8s_reader, prometheus_url, metrics_backend, logs_url, allow_partial,
        window_days, [trial_id], monitoring_services,
    )
    if collection.scrape_gap.has_gap:
        print(
            f"[{trial_id}]   !!! CẢNH BÁO scrape gap phát hiện tại: "
            f"{collection.scrape_gap.affected_targets} -- kết quả increase() có thể KHÔNG "
            f"đáng tin cậy cho các target này (xem TRAFFIC_COLLECTION_PIPELINE.md mục 2)."
        )
    print(f"[{trial_id}]   -> {len(collection.series)} traffic series, port: {_port_source_summary(collection.series)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{trial_id}.json"
    payload = {
        "trial_id": trial_id,
        "export_format": 2,
        "traffic_series": _to_jsonable(collection.series),
        "series_daily_counts": collection.daily_counts,
        "config_evidence": _to_jsonable(config_evidence),
        "ground_truth": {"|".join(k): v for k, v in ground_truth.items()},
        "scrape_gap": _to_jsonable(collection.scrape_gap),
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[{trial_id}] Đã ghi: {out_path}")
    return out_path


def _real_pairkeys(shared_namespace: str) -> set[tuple]:
    scenarios_dir = Path(__file__).resolve().parent.parent / "scenarios"
    if str(scenarios_dir) not in sys.path:
        sys.path.insert(0, str(scenarios_dir))
    from scenario_pool import ONLINE_BOUTIQUE_REAL_EDGES  # noqa: E402

    return {(shared_namespace, s, shared_namespace, d) for s, d in ONLINE_BOUTIQUE_REAL_EDGES}


def split_shared_collection(
    shared_namespace: str,
    virtual_trial_ids: list[str],
    owner_map: dict[tuple, list[str]],
    traffic_series: list,
    config_evidence: list,
    scrape_gap,
    ground_truth_dir: Path,
    out_dir: Path,
    daily_counts: list | None = None,
) -> list[Path]:
    """Phần THUẦN (không đụng K8s/Prometheus thật) của `export_shared_trials` — tách 1
    lần thu thập traffic/config CHUNG thành N `TrialInput` riêng, bằng cách so khớp
    `pair_key()` với `owner_map` -- KHÔNG dùng nhãn Prometheus `injection_id` (đã xác
    nhận không resolve được qua waypoint ambient, xem TRAFFIC_COLLECTION_PIPELINE.md mục
    3) -- pair_key là dữ liệu Python thuần, không phụ thuộc hạ tầng mesh. Tách hàm riêng
    (không gọi collector thật) để unit-test được, xem `test_export_shared.py`.

    1 series/config-evidence thuộc về virtual_trial_id X khi và chỉ khi:
      (a) pair_key của nó là 1 trong 15 cạnh REAL của Online Boutique (luôn True, DÙNG
          CHUNG cho MỌI virtual trial -- đúng nguyên tắc TrialScenario.ground_truth() đã
          áp dụng từ trước cho model namespace-riêng), HOẶC
      (b) X có mặt trong owner_map[pair_key] (cạnh cấy THUỘC VỀ virtual trial X -- có thể
          nhiều X cùng sở hữu 1 cạnh, xem SharedEdge.owners).
    Series/evidence không khớp (a) lẫn (b) (vd cạnh cấy CHỈ thuộc virtual trial khác) bị
    LOẠI khỏi TrialInput của X -- nếu không loại, Tier 1 sẽ thấy traffic không nằm trong
    ground_truth của X, bị tính nhầm thành false positive."""
    real_pairkeys = _real_pairkeys(shared_namespace)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_paths = []
    for vtid in virtual_trial_ids:
        gt_path = ground_truth_dir / f"{vtid}.json"
        if not gt_path.exists():
            print(f"[{vtid}] !!! không thấy ground truth '{gt_path}' -- bỏ qua.", file=sys.stderr)
            continue
        ground_truth = _load_ground_truth(gt_path)

        def _owned(pair_key: tuple, _vtid: str = vtid) -> bool:
            return pair_key in real_pairkeys or _vtid in owner_map.get(pair_key, [])

        daily = daily_counts if daily_counts is not None else [{} for _ in traffic_series]
        kept_idx = [i for i, s in enumerate(traffic_series) if _owned(s.identity_key().pair_key())]
        trial_traffic = [traffic_series[i] for i in kept_idx]
        trial_daily = [daily[i] for i in kept_idx]
        trial_config = [c for c in config_evidence if _owned(c.pair_key())]

        out_path = out_dir / f"{vtid}.json"
        payload = {
            "trial_id": vtid,
            "export_format": 2,
            "traffic_series": _to_jsonable(trial_traffic),
            "series_daily_counts": trial_daily,
            "config_evidence": _to_jsonable(trial_config),
            "ground_truth": {"|".join(k): v for k, v in ground_truth.items()},
            "scrape_gap": _to_jsonable(scrape_gap),
        }
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(
            f"[{vtid}]   -> {len(trial_traffic)} traffic series, {len(trial_config)} config "
            f"evidence (SAU khi tách). Đã ghi: {out_path}"
        )
        out_paths.append(out_path)

    return out_paths


def export_shared_trials(
    shared_namespace: str,
    virtual_trial_ids: list[str],
    owner_map: dict[tuple, list[str]],
    prometheus_url: str,
    ground_truth_dir: Path,
    out_dir: Path,
    window_days: int,
    kubeconfig_path: str | None,
    cache_db_path: str | None = None,
    metrics_backend: str | None = None,
    logs_url: str | None = None,
    allow_partial: bool = False,
    monitoring_services: tuple = ("istio-system/prometheus",),
) -> list[Path]:
    """Chế độ "hạ tầng dùng chung" (05_deploy_shared.sh) — 1 namespace K8s DUY NHẤT chứa
    1 Online Boutique thật + N virtual trial. CHỈ thu thập traffic/config MỘT LẦN từ
    `shared_namespace` (không phải N lần như model namespace-riêng) rồi giao cho
    `split_shared_collection` tách thành N `TrialInput` riêng (xem docstring hàm đó)."""
    from tier1.config_collector import KubernetesReader, collect_config_evidence

    print(f"[{shared_namespace}] Đọc config evidence từ cluster thật (1 LẦN, dùng chung cho {len(virtual_trial_ids)} virtual trial)...")
    k8s_reader = KubernetesReader(kubeconfig_path=kubeconfig_path)
    # cache_db_path -- xem comment ở export_one_trial: cache BỀN qua nhiều lần chạy script,
    # tránh gọi lại LLM (tốn tiền/token) cho chunk đã extract thành công ở lần chạy trước.
    if cache_db_path:
        Path(cache_db_path).parent.mkdir(parents=True, exist_ok=True)
    all_config_evidence = collect_config_evidence(
        k8s_reader, snapshot_id=f"{shared_namespace}-snapshot", namespaces=[shared_namespace], db_path=cache_db_path,
    )
    print(f"[{shared_namespace}]   -> {len(all_config_evidence)} config evidence record (TRƯỚC khi tách theo virtual trial)")

    print(f"[{shared_namespace}] Đọc traffic {window_days} ngày (1 LẦN, {metrics_backend or 'prometheus'}: "
          f"{prometheus_url}{', logs: ' + logs_url if logs_url else ''})...")
    collection = _collect_traffic_with_context(
        k8s_reader, prometheus_url, metrics_backend, logs_url, allow_partial,
        window_days, [shared_namespace], monitoring_services,
    )
    if collection.scrape_gap.has_gap:
        print(
            f"[{shared_namespace}]   !!! CẢNH BÁO scrape gap phát hiện tại: "
            f"{collection.scrape_gap.affected_targets} -- kết quả increase() có thể KHÔNG "
            f"đáng tin cậy cho các target này (xem TRAFFIC_COLLECTION_PIPELINE.md mục 2)."
        )
    print(f"[{shared_namespace}]   -> {len(collection.series)} traffic series (TRƯỚC khi tách theo virtual trial), "
          f"port: {_port_source_summary(collection.series)}, cạnh chỉ thấy qua log: {collection.log_series_added}")

    return split_shared_collection(
        shared_namespace=shared_namespace,
        virtual_trial_ids=virtual_trial_ids,
        owner_map=owner_map,
        traffic_series=collection.series,
        config_evidence=all_config_evidence,
        scrape_gap=collection.scrape_gap,
        ground_truth_dir=ground_truth_dir,
        out_dir=out_dir,
        daily_counts=collection.daily_counts,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trial_ids", nargs="*", help="Danh sách trial_id cần export, vd: trial-01 trial-02")
    parser.add_argument("--all", action="store_true", help="Export MỌI trial_id đang có file ground-truth")
    parser.add_argument("--prometheus-url", default="http://localhost:9090",
                        help="Prometheus, hoặc vmauth của LOG_METRIC: http://localhost:8427/select/0/prometheus")
    parser.add_argument("--metrics-backend", default=None, choices=["prometheus", "victoriametrics"],
                        help="mặc định: env TRAFFIC_METRICS_BACKEND, rồi prometheus")
    parser.add_argument("--allow-partial", action="store_true",
                        help="[victoriametrics] chấp nhận kết quả isPartial (mặc định: báo lỗi)")
    parser.add_argument("--logs-url", default=None,
                        help="VictoriaLogs/vmauth (vd http://localhost:8427) — đọc access log ztunnel")
    parser.add_argument("--monitoring-service", action="append", default=None,
                        help="ns/tên-service Prometheus/vmagent (lặp lại được; mặc định istio-system/prometheus)")
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--kubeconfig", default=None, help="Mặc định: dùng kubeconfig đang active (~/.kube/config)")
    parser.add_argument(
        "--tier1-path",
        default=str(Path(__file__).resolve().parent.parent.parent / "tier1_inference"),
        help="Thư mục chứa package tier1/ (mặc định: đoán ../../tier1_inference từ vị trí script này)",
    )
    parser.add_argument("--ground-truth-dir", default=str(Path(__file__).resolve().parent.parent / "ground_truth"))
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "trial_data"))
    parser.add_argument(
        "--cache-db",
        default=str(Path(__file__).resolve().parent.parent / ".cache" / "llm_extraction_cache.sqlite3"),
        help="File SQLite cache LLM BỀN qua nhiều lần CHẠY SCRIPT (mục 2/4.3) -- chunk đã "
        "extract thành công ở lần chạy trước (vd 1 trial lỗi phải export lại, hoặc dry-run "
        "nhiều lần) sẽ KHÔNG gọi lại LLM, tránh tốn token/tiền vô ích. Truyền rỗng ('') để "
        "tắt cache (chỉ cache trong bộ nhớ, mất khi script kết thúc, như hành vi CŨ).",
    )

    # --- Kiến trúc "dùng chung hạ tầng" (05_deploy_shared.sh) ---
    parser.add_argument(
        "--shared", action="store_true",
        help="Chế độ hạ tầng dùng chung -- thu thập traffic/config 1 LẦN từ --shared-namespace, "
        "tách thành N TrialInput bằng --owner-map (KHÔNG lặp lại collect cho mỗi trial_id).",
    )
    parser.add_argument("--shared-namespace", default="shared-eval", help="[--shared] Tên namespace K8s dùng chung.")
    parser.add_argument("--owner-map", default=None, help="[--shared] Đường dẫn owner_map.json (do 05_deploy_shared.sh sinh, xem thông báo cuối script đó).")
    args = parser.parse_args()

    _add_tier1_to_path(args.tier1_path)

    ground_truth_dir = Path(args.ground_truth_dir)
    trial_ids = list(args.trial_ids)
    if args.all:
        if not ground_truth_dir.exists():
            raise SystemExit(f"LỖI: --all nhưng không thấy thư mục ground-truth '{ground_truth_dir}'.")
        trial_ids = sorted(p.stem for p in ground_truth_dir.glob("*.json"))
    if not trial_ids:
        raise SystemExit("LỖI: cần truyền ít nhất 1 trial_id, hoặc dùng --all.")

    out_dir = Path(args.out_dir)
    cache_db_path = args.cache_db or None
    traffic_kwargs = dict(
        metrics_backend=args.metrics_backend, logs_url=args.logs_url, allow_partial=args.allow_partial,
        monitoring_services=tuple(args.monitoring_service or ("istio-system/prometheus",)),
    )

    if args.shared:
        if not args.owner_map:
            raise SystemExit("LỖI: --shared cần truyền --owner-map <path> (do 05_deploy_shared.sh sinh).")
        owner_map = _load_owner_map(Path(args.owner_map))
        export_shared_trials(
            shared_namespace=args.shared_namespace,
            virtual_trial_ids=trial_ids,
            owner_map=owner_map,
            prometheus_url=args.prometheus_url,
            ground_truth_dir=ground_truth_dir,
            out_dir=out_dir,
            window_days=args.window_days,
            kubeconfig_path=args.kubeconfig,
            cache_db_path=cache_db_path,
            **traffic_kwargs,
        )
        return
    failures = []
    for trial_id in trial_ids:
        try:
            export_one_trial(
                trial_id,
                prometheus_url=args.prometheus_url,
                ground_truth_dir=ground_truth_dir,
                out_dir=out_dir,
                window_days=args.window_days,
                kubeconfig_path=args.kubeconfig,
                cache_db_path=cache_db_path,
                **traffic_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — muốn tiếp tục export các trial còn lại dù 1 trial lỗi
            print(f"[{trial_id}] !!! LỖI khi export: {exc}", file=sys.stderr)
            failures.append(trial_id)

    print("")
    print(f"=== Xong: {len(trial_ids) - len(failures)}/{len(trial_ids)} trial export thành công ===")
    if failures:
        print(f"Thất bại: {failures}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
