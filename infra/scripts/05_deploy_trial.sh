#!/usr/bin/env bash
# Deploy 1 trial: namespace riêng + Online Boutique thật (upstream) + harness Deployment
# cấy traffic/config (scenarios/render_manifests.py). Idempotent (chạy lại an toàn).
#
# Dùng: ./05_deploy_trial.sh <trial_id> [seed]
#   trial_id  : PHẢI là RFC 1123 label hợp lệ (chữ thường/số/'-', KHÔNG dùng '_') vì
#               dùng LUÔN làm tên namespace K8s, vd: trial-01
#   seed      : tuỳ chọn, mặc định suy từ trial_id (xem scenario_pool.py::generate_trial)
#
# Nguồn manifest Online Boutique: xác nhận qua kiểm tra nội dung thật (2026-09-25) —
# 11 Deployment + Service (frontend, cartservice, productcatalogservice, currencyservice,
# paymentservice, shippingservice, emailservice, checkoutservice, recommendationservice,
# adservice, redis-cart) + ServiceAccount riêng cho TỪNG service (trừ redis-cart, dùng SA
# "default"). Đúng khớp SERVICE_PORTS/ONLINE_BOUTIQUE_SERVICES trong scenario_pool.py.
set -euo pipefail
export PATH="$HOME/bin:$PATH"

TRIAL_ID="${1:?Cần truyền trial_id, vd: ./05_deploy_trial.sh trial-01}"
SEED="${2:-}"

if ! [[ "$TRIAL_ID" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "LỖI: trial_id '$TRIAL_ID' không hợp lệ làm tên namespace K8s." >&2
  echo "     Chỉ chữ thường/số/dấu gạch ngang, KHÔNG dùng '_' (vd: trial-01, không phải trial_01)." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUND_TRUTH_DIR="$INFRA_DIR/ground_truth"
CACHE_DIR="$INFRA_DIR/.cache"
mkdir -p "$GROUND_TRUTH_DIR" "$CACHE_DIR"

ONLINE_BOUTIQUE_MANIFEST_URL="${ONLINE_BOUTIQUE_MANIFEST_URL:-https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml}"
ONLINE_BOUTIQUE_MANIFEST_CACHE="$CACHE_DIR/online-boutique-manifests.yaml"

echo "=== [1/6] Tạo namespace '$TRIAL_ID' (bật Istio ambient dataplane, KHÔNG dùng sidecar) ==="
kubectl create namespace "$TRIAL_ID" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$TRIAL_ID" istio.io/dataplane-mode=ambient --overwrite

echo "=== [2/6] Tạo waypoint L7 cho namespace (bắt buộc để có istio_requests_total — xem"
echo "    lý do ở scripts/03_install_istio.sh) và tự gắn nhãn use-waypoint cho namespace ==="
istioctl waypoint apply -n "$TRIAL_ID" --enroll-namespace --wait

echo "=== [3/6] Tải manifest Online Boutique gốc (cache tại .cache/, xoá file để tải lại) ==="
if [ ! -f "$ONLINE_BOUTIQUE_MANIFEST_CACHE" ]; then
  curl -fsSL "$ONLINE_BOUTIQUE_MANIFEST_URL" -o "$ONLINE_BOUTIQUE_MANIFEST_CACHE"
fi

echo "=== [4/6] Deploy Online Boutique thật vào namespace '$TRIAL_ID' ==="
kubectl apply -n "$TRIAL_ID" -f "$ONLINE_BOUTIQUE_MANIFEST_CACHE"

echo "=== [5/6] Đảm bảo ServiceAccount tồn tại cho MỌI identity cần impersonate ==="
# Online Boutique tự tạo SA cho 10/11 service (trừ redis-cart) -- nhưng harness còn cần
# impersonate cả 3 "identity" monitoring giả lập (NOISE_MONITORING, không phải service
# thật nên chắc chắn KHÔNG có sẵn SA). Lặp lại toàn bộ 14 tên cho chắc, idempotent
# (kubectl apply lên SA đã tồn tại là no-op).
for sa in frontend cartservice productcatalogservice currencyservice paymentservice \
          shippingservice emailservice checkoutservice recommendationservice adservice \
          redis-cart prometheus grafana blackbox-exporter; do
  kubectl create serviceaccount "$sa" -n "$TRIAL_ID" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

echo "=== [6/6] Sinh & áp harness Deployment (traffic/config cấy theo kịch bản) ==="
SEED_ARGS=()
if [ -n "$SEED" ]; then
  SEED_ARGS=(--seed "$SEED")
fi
python3 "$SCRIPT_DIR/../scenarios/render_manifests.py" "$TRIAL_ID" "${SEED_ARGS[@]}" \
  --dump-ground-truth "$GROUND_TRUTH_DIR/$TRIAL_ID.json" \
  | kubectl apply -n "$TRIAL_ID" -f -

echo ""
echo "=> Đã deploy xong trial '$TRIAL_ID'."
echo "   Ground truth đã lưu: $GROUND_TRUTH_DIR/$TRIAL_ID.json (dùng cho tier1/experiment.py sau này)"
echo "   Kiểm tra trạng thái: kubectl get pods -n $TRIAL_ID (lưu ý pod báo 1/1, KHÔNG phải"
echo "   2/2 — ambient không chèn container sidecar thứ 2 vào pod, xem 03_install_istio.sh)."
echo "   Chạy 07_verify.sh để xác nhận ztunnel/waypoint/nhãn/Prometheus trước khi tính"
echo "   'ngày 0' của cửa sổ quan sát 7 ngày (window_days trong tier1/config.py)."
