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
    GRPC  -> `fullstorydev/grpcurl`. STRONG_HIDDEN/SPARSE_HIDDEN gọi 1 method NGHIỆP VỤ thật
             qua demo.proto (GRPC_BUSINESS_CALLS, sửa 02/11). NOISE_MONITORING vẫn dùng
             `list` (server reflection). LƯU Ý: giả định cũ "Online Boutique bật reflection
             theo mặc định" là SAI — chỉ shippingservice đăng ký reflection, nên các lời gọi
             `list` tới service khác lỗi 100% (đo thật 20–26/10: error_ratio = 1,0).
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
from typing import Optional

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
# Gọi $CMD lặp lại mỗi $INTERVAL giây.
#   ACTIVE_DATES (ưu tiên, 02/11): danh sách ngày lịch UTC "YYYY-MM-DD,..." -- chỉ gọi vào
#     đúng những ngày đó, KHÔNG phụ thuộc lúc container start (pod restart / VM tắt-bật
#     không làm lệch lịch như ACTIVE_DAYS).
#   ACTIVE_DAYS (cũ): số ngày đếm từ lúc container start -- rỗng = gọi mọi ngày.
set -eu
START_EPOCH=$(date +%s)
while true; do
  NOW=$(date +%s)
  ELAPSED_DAYS=$(( (NOW - START_EPOCH) / 86400 ))
  TODAY=$(date -u +%Y-%m-%d)
  RUN=0
  if [ -n "${ACTIVE_DATES:-}" ]; then
    if echo ",${ACTIVE_DATES}," | grep -q ",${TODAY},"; then RUN=1; fi
  elif [ -z "${ACTIVE_DAYS:-}" ] || echo ",${ACTIVE_DAYS}," | grep -q ",${ELAPSED_DAYS},"; then
    RUN=1
  fi
  if [ "$RUN" = 1 ]; then
    sh -c "${CALL_CMD}" >/tmp/last_call.log 2>&1 || true
  fi
  sleep "${INTERVAL_SECONDS}"
done
"""

# Lời gọi gRPC NGHIỆP VỤ cho cạnh cấy STRONG_HIDDEN/SPARSE_HIDDEN (02/11). Trước đó harness
# dùng `grpcurl list` (server reflection) -- nhưng trong Online Boutique chỉ shippingservice
# đăng ký reflection, nên mọi cạnh cấy tới service khác lỗi 100% (UNIMPLEMENTED) và path
# quan sát được là /grpc.reflection.../ServerReflectionInfo chứ không phải 1 method nghiệp
# vụ -- khác hẳn 1 dependency thật. Bản này gọi method thật qua file demo.proto (mount từ
# ConfigMap `harness-protos`), request tối thiểu hợp lệ, ưu tiên method chỉ đọc.
# Tên biến môi trường địa chỉ theo đúng quy ước của chính Online Boutique (*_SERVICE_ADDR).
_TEST_CARD = {"credit_card_number": "4432-8015-6152-0454", "credit_card_cvv": 672,
              "credit_card_expiration_year": 2039, "credit_card_expiration_month": 1}
_ADDRESS = {"street_address": "1600 Amphitheatre Parkway", "city": "Mountain View", "state": "CA",
            "country": "United States", "zip_code": 94043}
GRPC_BUSINESS_CALLS: dict[str, tuple[str, str, dict]] = {
    # đích -> (biến môi trường địa chỉ, method đầy đủ, request JSON)
    "productcatalogservice": ("PRODUCT_CATALOG_SERVICE_ADDR", "hipstershop.ProductCatalogService/ListProducts", {}),
    "currencyservice": ("CURRENCY_SERVICE_ADDR", "hipstershop.CurrencyService/GetSupportedCurrencies", {}),
    "cartservice": ("CART_SERVICE_ADDR", "hipstershop.CartService/GetCart", {"user_id": "batch-reporter"}),
    "recommendationservice": ("RECOMMENDATION_SERVICE_ADDR", "hipstershop.RecommendationService/ListRecommendations",
                              {"user_id": "batch-reporter", "product_ids": ["OLJCESPC7Z"]}),
    "shippingservice": ("SHIPPING_SERVICE_ADDR", "hipstershop.ShippingService/GetQuote",
                        {"address": _ADDRESS, "items": [{"product_id": "OLJCESPC7Z", "quantity": 1}]}),
    "adservice": ("AD_SERVICE_ADDR", "hipstershop.AdService/GetAds", {"context_keys": ["kitchen"]}),
    "paymentservice": ("PAYMENT_SERVICE_ADDR", "hipstershop.PaymentService/Charge",
                       {"amount": {"currency_code": "USD", "units": 1}, "credit_card": _TEST_CARD}),
    "emailservice": ("EMAIL_SERVICE_ADDR", "hipstershop.EmailService/SendOrderConfirmation",
                     {"email": "reports@example.com", "order": {"order_id": "batch-report"}}),
    "checkoutservice": ("CHECKOUT_SERVICE_ADDR", "hipstershop.CheckoutService/PlaceOrder",
                        {"user_id": "batch-reporter", "user_currency": "USD", "address": _ADDRESS,
                         "email": "reports@example.com", "credit_card": _TEST_CARD}),
}
_BUSINESS_KINDS = (ScenarioKind.STRONG_HIDDEN, ScenarioKind.SPARSE_HIDDEN)


def _uses_business_grpc(edge: InjectedEdge) -> bool:
    return (edge.kind in _BUSINESS_KINDS and edge.requests_per_minute is not None
            and edge.destination_port_protocol[1] == "GRPC" and edge.destination in GRPC_BUSINESS_CALLS)


def sparse_active_dates(edge: InjectedEdge, window_start: str, window_days: int = 7) -> list[str]:
    """Chọn `active_days_out_of_window` ngày lịch (UTC) trong cửa sổ [window_start,
    window_start + window_days) cho 1 cạnh SPARSE_HIDDEN -- tất định theo cặp
    (source, destination), KHÔNG dùng rng của scenario_pool (giữ nguyên chuỗi rút cặp)."""
    import hashlib
    from datetime import date, timedelta

    start = date.fromisoformat(window_start)
    n = max(1, min(int(edge.active_days_out_of_window or 1), window_days))
    seed = int(hashlib.sha256(f"{edge.source}->{edge.destination}".encode()).hexdigest(), 16)
    offsets = list(range(window_days))
    picked = []
    while len(picked) < n:
        idx = seed % len(offsets)
        picked.append(offsets.pop(idx))
        seed //= 7
    return [(start + timedelta(days=o)).isoformat() for o in sorted(picked)]


def _call_cmd(edge: InjectedEdge, namespace: str) -> str:
    port, protocol = edge.destination_port_protocol
    host = f"{edge.destination}.{namespace}.svc.cluster.local"
    if protocol == "HTTP":
        return f"curl -s -o /dev/null -m 5 'http://{host}:{port}{edge.path or '/'}'"
    if protocol == "GRPC" and _uses_business_grpc(edge):
        addr_env, method, _ = GRPC_BUSINESS_CALLS[edge.destination]
        return (f'grpcurl -plaintext -connect-timeout 5 -import-path /protos -proto demo.proto '
                f'-d "$GRPC_REQUEST" "${addr_env}" {method}')
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


def _harness_deployment_yaml(edge: InjectedEdge, namespace: str, trial_id: str, service_account_hint: str,
                             sparse_window_start: Optional[str] = None, window_days: int = 7) -> str:
    name = _k8s_safe_name(f"harness-{edge.kind.value.lower().replace('_', '-')}-{edge.source}-{edge.destination}")
    has_traffic = edge.requests_per_minute is not None
    interval_seconds = max(1, 60 // edge.requests_per_minute) if has_traffic else 3600
    is_sparse = has_traffic and edge.kind == ScenarioKind.SPARSE_HIDDEN
    active_dates = ",".join(sparse_active_dates(edge, sparse_window_start, window_days)) if (
        is_sparse and sparse_window_start
    ) else ""
    active_days = ",".join(str(d) for d in range(edge.active_days_out_of_window)) if (
        is_sparse and not active_dates
    ) else ""
    # STRONG_HIDDEN/NOISE_* : active mọi ngày trong cửa sổ -> active_days rỗng (luôn gọi)

    port, protocol = edge.destination_port_protocol
    image = _image_for_protocol(protocol) if has_traffic else "busybox:1.36"
    call_cmd = _call_cmd(edge, namespace) if has_traffic else "true"
    business = has_traffic and _uses_business_grpc(edge)

    def _env(name_: str, value: str) -> list[str]:
        return [f"        - name: {name_}", f"          value: {json.dumps(value, ensure_ascii=False)}"]

    env_lines = []
    if active_dates:
        env_lines += _env("ACTIVE_DATES", active_dates)
    else:
        env_lines += _env("ACTIVE_DAYS", active_days)
    env_lines += _env("INTERVAL_SECONDS", str(interval_seconds))
    if business:
        addr_env, _, request = GRPC_BUSINESS_CALLS[edge.destination]
        env_lines += _env(addr_env, f"{edge.destination}:{port}")
        env_lines += _env("GRPC_REQUEST", json.dumps(request, separators=(",", ":")))
    env_lines += _env("CALL_CMD", call_cmd)
    if edge.declare_config_reference:
        target = edge.destination if edge.config_reference_resolvable else f"{edge.destination}-nonexistent"
        # Biến môi trường đại diện cho "config evidence" mà config_collector.py sẽ đọc
        # được (mục 4 CONFIG_EVIDENCE_COLLECTOR.md, cấp 3 - chỉ tên trần).
        env_lines += _env("HIDDEN_DEP_TARGET", target)

    command = (
        "sleep infinity" if not has_traffic
        else "sh /scripts/wrapper.sh"
    )
    command_block = (
        f'command: ["sh", "-c", "{command}"]' if not has_traffic
        else 'command: ["sh", "/scripts/wrapper.sh"]'
    )

    proto_mount = """
        - name: harness-protos
          mountPath: /protos""" if business else ""
    proto_volume = """
      - name: harness-protos
        configMap:
          name: harness-protos""" if business else ""
    volume_mount = (
        ""
        if not has_traffic
        else f"""
        volumeMounts:
        - name: wrapper-script
          mountPath: /scripts{proto_mount}
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
          defaultMode: 0755{proto_volume}
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


def render_shared(scenario: SharedInfraScenario, service_account_hint: str = "{source}",
                  sparse_window_start: Optional[str] = None, window_days: int = 7) -> str:
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
                sparse_window_start=sparse_window_start, window_days=window_days,
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
        "--sparse-window-start", default=None, metavar="YYYY-MM-DD",
        help="[--shared] Ngày đầu cửa sổ đánh giá (UTC). Có tham số này thì cạnh SPARSE_HIDDEN "
        "chạy theo NGÀY LỊCH (ACTIVE_DATES) trong cửa sổ thay vì đếm từ lúc pod start.",
    )
    parser.add_argument("--window-days", type=int, default=7, help="[--shared] Số ngày cửa sổ (đi cùng --sparse-window-start).")
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
        print(render_shared(scenario, service_account_hint=args.service_account,
                            sparse_window_start=args.sparse_window_start, window_days=args.window_days))

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
