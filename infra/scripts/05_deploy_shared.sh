#!/usr/bin/env bash
# Deploy kiến trúc "DÙNG CHUNG HẠ TẦNG" (thay cho 05_deploy_trial.sh + 06_deploy_all_trials.sh):
# 1 namespace K8s DUY NHẤT chứa 1 Online Boutique thật (deploy 1 LẦN, không phải x15) +
# toàn bộ harness Deployment thật cho N "virtual trial" (scenario_pool.generate_shared_trials).
# Idempotent (chạy lại an toàn).
#
# VÌ SAO SCRIPT NÀY TỒN TẠI: check_capacity.sh xác nhận THẬT (2026-09-27, máy Bin) model
# namespace-riêng (05_deploy_trial.sh x15 qua 06_deploy_all_trials.sh) chỉ chạy nổi tối đa
# 4/15 trial đồng thời trên laptop thật (giới hạn RAM) — phần chiếm tài nguyên lớn nhất là
# APP CONTAINER (Online Boutique thật x15 bản), không phải mesh proxy (ambient đã cắt gần
# hết phần đó, xem README "Sizing"). Hướng đã chọn cùng cô Dung: app chỉ chạy 1 LẦN.
#
# Dùng: ./05_deploy_shared.sh [namespace] [n_trials] [base_seed]
#   namespace  : mặc định "shared-eval" — PHẢI là RFC 1123 label hợp lệ
#   n_trials   : mặc định 15
#   base_seed  : mặc định 20260101 (khớp mặc định generate_shared_trials)
set -euo pipefail
export PATH="$HOME/bin:$PATH"

NAMESPACE="${1:-shared-eval}"
N_TRIALS="${2:-15}"
BASE_SEED="${3:-20260101}"

if ! [[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "LỖI: namespace '$NAMESPACE' không hợp lệ làm tên namespace K8s." >&2
  echo "     Chỉ chữ thường/số/dấu gạch ngang, KHÔNG dùng '_'." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GROUND_TRUTH_DIR="$INFRA_DIR/ground_truth"
CACHE_DIR="$INFRA_DIR/.cache"
mkdir -p "$GROUND_TRUTH_DIR" "$CACHE_DIR"

ONLINE_BOUTIQUE_MANIFEST_URL="${ONLINE_BOUTIQUE_MANIFEST_URL:-https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml}"
ONLINE_BOUTIQUE_MANIFEST_CACHE="$CACHE_DIR/online-boutique-manifests.yaml"

echo "=== [1/6] Tạo namespace '$NAMESPACE' (bật Istio ambient dataplane, KHÔNG dùng sidecar) ==="
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NAMESPACE" istio.io/dataplane-mode=ambient --overwrite

echo "=== [2/6] Tạo waypoint L7 cho namespace (1 LẦN DUY NHẤT, dùng chung cho cả Online"
echo "    Boutique thật lẫn toàn bộ harness — xem lý do ở scripts/03_install_istio.sh) ==="
istioctl waypoint apply -n "$NAMESPACE" --enroll-namespace --wait

echo "=== [3/6] Tải manifest Online Boutique gốc (cache tại .cache/, xoá file để tải lại) ==="
if [ ! -f "$ONLINE_BOUTIQUE_MANIFEST_CACHE" ]; then
  curl -fsSL "$ONLINE_BOUTIQUE_MANIFEST_URL" -o "$ONLINE_BOUTIQUE_MANIFEST_CACHE"
fi

echo "=== [4/6] Deploy Online Boutique thật vào namespace '$NAMESPACE' (1 LẦN, không phải x N trial) ==="
kubectl apply -n "$NAMESPACE" -f "$ONLINE_BOUTIQUE_MANIFEST_CACHE"

echo "=== [5/6] Đảm bảo ServiceAccount tồn tại cho MỌI identity cần impersonate ==="
for sa in frontend cartservice productcatalogservice currencyservice paymentservice \
          shippingservice emailservice checkoutservice recommendationservice adservice \
          redis-cart prometheus grafana blackbox-exporter; do
  kubectl create serviceaccount "$sa" -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

echo "=== [6/6] Sinh & áp TOÀN BỘ harness Deployment cho $N_TRIALS virtual trial ==="
# LƯU Ý: owner_map PHẢI nằm NGOÀI $GROUND_TRUTH_DIR -- 08_export_trial_data.py --all liệt
# kê trial_id bằng cách glob "*.json" trong đúng thư mục đó, để owner_map lẫn vào sẽ bị
# hiểu nhầm thành 1 "trial_id" giả.
OWNER_MAP_PATH="$CACHE_DIR/${NAMESPACE}.owner_map.json"
python3 "$SCRIPT_DIR/../scenarios/render_manifests.py" --shared \
  --namespace "$NAMESPACE" --n-trials "$N_TRIALS" --base-seed "$BASE_SEED" \
  --dump-ground-truth-dir "$GROUND_TRUTH_DIR" \
  --dump-owner-map "$OWNER_MAP_PATH" \
  | kubectl apply -n "$NAMESPACE" -f -

echo ""
echo "=> Đã deploy xong hạ tầng dùng chung, namespace '$NAMESPACE', $N_TRIALS virtual trial."
echo "   Ground truth mỗi virtual trial: $GROUND_TRUTH_DIR/trial-XX.json (CÙNG định dạng"
echo "   model namespace-riêng — 08_export_trial_data.py không cần đổi cách đọc)."
echo "   Owner map (dùng để tách traffic/config theo virtual trial): $OWNER_MAP_PATH"
echo "   Kiểm tra trạng thái: kubectl get pods -n $NAMESPACE (pod báo 1/1, KHÔNG phải 2/2)."
echo "   Chạy 07_verify.sh $NAMESPACE để xác nhận ztunnel/waypoint/request_path/Prometheus"
echo "   trước khi tính 'ngày 0' của cửa sổ quan sát 7 ngày (window_days trong tier1/config.py)."
echo ""
echo "   Xuất dữ liệu sau 7 ngày, dùng chế độ --shared của 08_export_trial_data.py:"
echo "     python3 08_export_trial_data.py --shared --shared-namespace $NAMESPACE --owner-map \"$OWNER_MAP_PATH\" --all"
