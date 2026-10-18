"""
Sinh YAML K8s cho 1 trial từ `TrialScenario` (scenario_pool.py) — namespace + harness
Deployment cho từng cạnh cấy. In ra stdout, dùng với `kubectl apply -f -`.

CHƯA kiểm chứng trên cluster thật (không có cluster trong sandbox soạn tài liệu này).
Thiết kế/đánh đổi đã chọn, cần biết khi debug:

- Mesh mode: dùng Istio **ambient** (ztunnel + waypoint), KHÔNG dùng sidecar (xem lý do ở
  docstring `scripts/03_install_istio.sh`). Namespace gắn nhãn
  `istio.io/dataplane-mode: ambient` (thay `istio-injection: enabled` cũ) — pod sẽ CHỈ có
  1 container (`kubectl get pods` báo `1/1`, không phải `2/2` như sidecar).
- Danh tính (source impersonation): gán nhãn `service.istio.io/canonical-name: <source>`
  lên pod harness — Istio (ztunnel/waypoint, không còn sidecar injector) đọc nhãn này để
  gán `source_canonical_service`/`destination_canonical_service` trên metric (identity mà
  `config_collector.py`/`traffic_collector.py` đọc lại) — quy ước nhãn KHÔNG đổi giữa
  sidecar và ambient, chỉ nơi đọc đổi. `serviceAccountName` mặc định thử `<source>` — CẦN
  xác nhận lại sau khi Online Boutique đã deploy thật (`kubectl get pods -n <ns> -o
  jsonpath='{.items[*].spec.serviceAccountName}'`), sửa `--service-account` nếu khác.
- Sinh traffic theo protocol đích (`SERVICE_PORTS` trong scenario_pool.py):
    HTTP  -> `curlimages/curl`, gọi thẳng path
    GRPC  -> `fullstorydev/grpcurl`, dùng `list` qua gRPC server reflection (Online
             Boutique bật reflection theo mặc định) — KHÔNG cần biết tên method thật,
             nhưng vẫn là 1 request gRPC thật, Envoy ghi nhận đúng `istio_requests_total`.
    TCP   -> `busybox`, `nc -z` (bắt tay TCP thuần, không cần nói đúng giao thức Redis)
- Lịch chạy sparse (SPARSE_HIDDEN, `active_days_out_of_window`): 1 wrapper script POSIX
  `sh` (mount qua ConfigMap) tính số ngày đã trôi qua kể từ lúc pod khởi động, chỉ thật
  sự gọi vào những ngày được liệt kê trong `--active-days` — CẦN pod sống liên tục suốt
  cả cửa sổ K ngày (không được để pod bị restart giữa chừng, vì mốc "ngày 0" tính lại
  từ lúc container start).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenario_pool import (  # noqa: E402
    InjectedEdge,
    ScenarioKind,
    SharedInfraScenario,
    TrialScenario,
    generate_shared_trials,
    generate_trial,
)

# RFC 1123 label: chữ thường/số/dấu gạch ngang, không bắt đầu/kết thúc bằng "-". Namespace
# VÀ mọi metadata.name trong file này đều phải khớp -- kubectl từ chối thẳng lúc apply nếu
# không (vd "trial_01" chứa "_" bị lỗi "must be a lowercase RFC 1123 label"), nên kiểm tra
# ở đây để báo lỗi sớm, rõ ràng, thay vì để apply nửa chừng rồi mới biết.
_RFC1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _validate_k8s_name(name: str, what: str) -> None:
    if not _RFC1123_LABEL.match(name):
        raise ValueError(
            f"{what} không hợp lệ: {name!r} — phải là RFC 1123 label (chữ thường, số, "
            f"dấu gạch ngang, không bắt đầu/kết thúc bằng '-', KHÔNG dùng '_')."
        )


def _k8s_safe_name(name: str, max_len: int = 63) -> str:
    """Cắt về tối đa `max_len` ký tự (giới hạn DNS-1123) và loại bỏ dấu '-' còn sót lại
    ở cuối sau khi cắt (cắt giữa chừng có thể để lại '-' cuối, không hợp lệ)."""
    return name[:max_len].rstrip("-")

WRAPPER_SCRIPT = r"""#!/bin/sh
# Gọi $CMD lặp lại mỗi $INTERVAL giây, CHỈ vào những ngày trong $ACTIVE_DAYS (đếm từ
# lúc container này start) -- rỗng = gọi mọi ngày. Dùng chung cho mọi harness Pod.
set -eu
START_EPOCH=$(date +%s)
while true; do
  NOW=$(date +%s)
  ELAPSED_DAYS=$(( (NOW - START_EPOCH) / 86400 ))
  if [ -z "${ACTIVE_DAYS:-}" ] || echo ",${ACTIVE_DAYS}," | grep -q ",${ELAPSED_DAYS},"; then
    sh -c "${CALL_CMD}" >/tmp/last_call.log 2>&1 || true
  fi
  sleep "${INTERVAL_SECONDS}"
done
"""


def _call_cmd(edge: InjectedEdge, namespace: str) -> str:
    port, protocol = edge.destination_port_protocol
    host = f"{edge.destination}.{namespace}.svc.cluster.local"
    if protocol == "HTTP":
        return f"curl -s -o /dev/null -m 5 'http://{host}:{port}{edge.path or '/'}'"
    if protocol == "GRPC":
        return f"grpcurl -plaintext -connect-timeout 5 {host}:{port} list"
    return f"nc -z -w2 {host} {port}"


def _image_for_protocol(protocol: str) -> str:
    return {
        "HTTP": "curlimages/curl:8.10.1",
        # KHÔNG dùng "fullstorydev/grpcurl:latest" (Docker Hub) -- ĐÃ XÁC NHẬN THẬT (chạy
        # trên máy Bin, 2026-09-27) ảnh đó build FROM scratch (xác nhận qua Dockerfile chính
        # thức trên GitHub), CHỈ có đúng /bin/grpcurl + ca-certificates -- KHÔNG có /bin/sh.
        # wrapper.sh (bài toán lịch chạy sparse/strong theo ACTIVE_DAYS, xem WRAPPER_SCRIPT)
        # cần "sh" để chạy -- pod CrashLoopBackOff/RunContainerError ngay khi start (đúng 3/6
        # harness pod cấy vào dest GRPC của trial-01 bị lỗi này: STRONG_HIDDEN, SPARSE_HIDDEN,
        # NOISE_MONITORING). Dùng thay ảnh "ghcr.io/fullstorydev/grpcurl:latest-alpine" --
        # xác nhận qua chính workflow release CI của dự án (.github/workflows/release.yml)
        # có build & push CẢ 2 target Dockerfile: "scratch" (mặc định, :latest) VÀ "alpine"
        # (:latest-alpine) lên ghcr.io -- bản alpine build từ `alpine:3`, có sẵn `/bin/sh`
        # (busybox) + `/bin/grpcurl` (đã nằm trong PATH mặc định của alpine), không cần sửa
        # gì thêm ở `_call_cmd` bên trên.
        "GRPC": "ghcr.io/fullstorydev/grpcurl:latest-alpine",
        "TCP": "busybox:1.36",
    }[protocol]


def _harness_deployment_yaml(edge: InjectedEdge, namespace: str, trial_id: str, service_account_hint: str) -> str:
    name = _k8s_safe_name(f"harness-{edge.kind.value.lower().replace('_', '-')}-{edge.source}-{edge.destination}")
    has_traffic = edge.requests_per_minute is not None
    interval_seconds = max(1, 60 // edge.requests_per_minute) if has_traffic else 3600
    active_days = ",".join(str(d) for d in range(edge.active_days_out_of_window)) if (
        has_traffic and edge.kind == ScenarioKind.SPARSE_HIDDEN
    ) else ""
    # STRONG_HIDDEN/NOISE_* : active mọi ngày trong cửa sổ -> active_days rỗng (luôn gọi)

    port, protocol = edge.destination_port_protocol
    image = _image_for_protocol(protocol) if has_traffic else "busybox:1.36"
    call_cmd = _call_cmd(edge, namespace) if has_traffic else "true"

    env_lines = [
        "        - name: ACTIVE_DAYS",
        f'          value: "{active_days}"',
        "        - name: INTERVAL_SECONDS",
        f'          value: "{interval_seconds}"',
        "        - name: CALL_CMD",
        f'          value: "{call_cmd}"',
    ]
    if edge.declare_config_reference:
        target = edge.destination if edge.config_reference_resolvable else f"{edge.destination}-nonexistent"
        # Biến môi trường đại diện cho "config evidence" mà config_collector.py sẽ đọc
        # được (mục 4 CONFIG_EVIDENCE_COLLECTOR.md, cấp 3 - chỉ tên trần).
        env_lines += [
            "        - name: HIDDEN_DEP_TARGET",
            f'          value: "{target}"',
        ]

    command = (
        "sleep infinity" if not has_traffic
        else "sh /scripts/wrapper.sh"
    )
    command_block = (
        f'command: ["sh", "-c", "{command}"]' if not has_traffic
        else 'command: ["sh", "/scripts/wrapper.sh"]'
    )

    volume_mount = (
        ""
        if not has_traffic
        else """
        volumeMounts:
        - name: wrapper-script
          mountPath: /scripts
"""
    )
    volume_block = (
        ""
        if not has_traffic
        else f"""
      volumes:
      - name: wrapper-script
        configMap:
          name: harness-wrapper-script
          defaultMode: 0755
"""
    )

    return f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app: {name}
    kltn/scenario-kind: "{edge.kind.value}"
    kltn/trial-id: "{trial_id}"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
        service.istio.io/canonical-name: "{edge.source}"
        test-harness: "true"
        injection-id: "{trial_id}"
    spec:
      serviceAccountName: {service_account_hint.format(source=edge.source)}
      containers:
      - name: harness
        image: {image}
        {command_block}
        env:
{chr(10).join(env_lines)}
        {volume_mount.strip()}
      {volume_block.strip()}
"""


def render(scenario: TrialScenario, namespace: str, service_account_hint: str = "{source}") -> str:
    _validate_k8s_name(namespace, "namespace")
    parts = [
        f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
  labels:
    istio.io/dataplane-mode: ambient
    kltn/trial-id: "{scenario.trial_id}"
""",
        f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: harness-wrapper-script
  namespace: {namespace}
data:
  wrapper.sh: |
{chr(10).join('    ' + line for line in WRAPPER_SCRIPT.splitlines())}
""",
    ]
    for edge in scenario.injected_edges:
        parts.append(_harness_deployment_yaml(edge, namespace, scenario.trial_id, service_account_hint))
    return "---".join(parts)


def render_shared(scenario: SharedInfraScenario, service_account_hint: str = "{source}") -> str:
    """Render YAML cho kiến trúc "dùng chung hạ tầng" (namespace K8s DUY NHẤT cho toàn bộ
    N virtual trial) — xem docstring `scenario_pool.SharedInfraScenario`. KHÁC `render()`:
    không render Online Boutique (script deploy áp riêng, 1 lần, xem `05_deploy_shared.sh`)
    — hàm này CHỈ render Namespace + ConfigMap wrapper + toàn bộ harness Deployment thật
    (đã khử trùng lặp ở tầng `generate_shared_trials`, mỗi `SharedEdge` = đúng 1 Deployment
    dù có thể nhiều virtual trial owner)."""
    _validate_k8s_name(scenario.namespace, "namespace")
    parts = [
        f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {scenario.namespace}
  labels:
    istio.io/dataplane-mode: ambient
    kltn/shared-infra: "true"
""",
        f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: harness-wrapper-script
  namespace: {scenario.namespace}
data:
  wrapper.sh: |
{chr(10).join('    ' + line for line in WRAPPER_SCRIPT.splitlines())}
""",
    ]
    for shared_edge in scenario.shared_edges:
        parts.append(
            _harness_deployment_yaml(
                shared_edge.edge, scenario.namespace, trial_id=scenario.namespace,
                service_account_hint=service_account_hint,
            )
        )
    return "---".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trial_id", nargs="?",
        help='vd: "trial-01" — cũng dùng làm tên namespace. BỎ QUA nếu dùng --shared.',
    )
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--service-account",
        default="{source}",
        help="Mẫu tên ServiceAccount, {source} sẽ được thay bằng tên service (mặc định: chính tên source)",
    )
    parser.add_argument("--dump-ground-truth", metavar="PATH", help="Ghi ground_truth JSON ra file (dùng cho experiment.py sau này)")

    # --- Kiến trúc "dùng chung hạ tầng" (xem scenario_pool.generate_shared_trials) ---
    parser.add_argument(
        "--shared", action="store_true",
        help="Sinh 1 namespace DUY NHẤT chứa N virtual trial (thay vì 1 trial_id = 1 namespace).",
    )
    parser.add_argument("--namespace", default="shared-eval", help="[--shared] Tên namespace K8s dùng chung.")
    parser.add_argument("--n-trials", type=int, default=15, help="[--shared] Số virtual trial.")
    parser.add_argument("--base-seed", type=int, default=20260101, help="[--shared] Seed sinh toàn bộ N virtual trial.")
    parser.add_argument(
        "--dump-ground-truth-dir", metavar="DIR",
        help="[--shared] Ghi 1 file ground_truth JSON MỖI virtual trial vào thư mục này "
        "(tên file <virtual_trial_id>.json — CÙNG định dạng/tên với --dump-ground-truth "
        "của model namespace-riêng, để 08_export_trial_data.py không cần đổi cách đọc).",
    )
    parser.add_argument(
        "--dump-owner-map", metavar="PATH",
        help="[--shared] Ghi owner_map JSON (pair_key -> [virtual_trial_id,...]) — "
        "08_export_trial_data.py dùng để tách 1 lần thu thập traffic/config CHUNG thành "
        "N TrialInput riêng, KHÔNG dựa vào nhãn Prometheus injection_id (đã xác nhận "
        "không hoạt động qua waypoint ambient, xem TRAFFIC_COLLECTION_PIPELINE.md mục 3).",
    )
    args = parser.parse_args()

    if args.shared:
        scenario = generate_shared_trials(n_trials=args.n_trials, base_seed=args.base_seed, namespace=args.namespace)
        print(render_shared(scenario, service_account_hint=args.service_account))

        if args.dump_ground_truth_dir:
            out_dir = Path(args.dump_ground_truth_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            for vtid in scenario.virtual_trial_ids:
                gt = {"|".join(k): v for k, v in scenario.ground_truth_for(vtid).items()}
                (out_dir / f"{vtid}.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False))

        if args.dump_owner_map:
            om = {"|".join(k): v for k, v in scenario.owner_map().items()}
            Path(args.dump_owner_map).write_text(json.dumps(om, indent=2, ensure_ascii=False))
        return

    if not args.trial_id:
        raise SystemExit("LỖI: cần truyền trial_id (hoặc dùng --shared cho model dùng chung hạ tầng).")

    scenario = generate_trial(args.trial_id, seed=args.seed)
    print(render(scenario, namespace=args.trial_id, service_account_hint=args.service_account))

    if args.dump_ground_truth:
        gt = {"|".join(k): v for k, v in scenario.ground_truth(namespace=args.trial_id).items()}
        Path(args.dump_ground_truth).write_text(json.dumps(gt, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
