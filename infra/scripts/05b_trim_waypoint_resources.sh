#!/usr/bin/env bash
# TÙY CHỌN, chạy SAU 05_deploy_trial.sh cho từng trial (đã tạo waypoint qua
# `istioctl waypoint apply`): hạ REQUEST (không đổi LIMIT, không đổi hành vi/traffic thật)
# của Deployment "waypoint" trong 1 namespace/trial.
#
# THAY THẾ 05b_trim_sidecar_resources.sh CŨ (dành cho mesh sidecar) — không còn áp dụng vì
# đã chuyển sang ambient (03_install_istio.sh): không còn container sidecar trong TỪNG pod
# để trim nữa, cả namespace giờ chỉ có 1 Deployment "waypoint" DÙNG CHUNG. Vì vậy script
# này chỉ cần patch ĐÚNG 1 Deployment/trial (thay vì lặp qua N pod như bản cũ) — bản thân
# đây đã là phần lớn lý do ambient nhẹ hơn nhiều, script này chỉ là tối ưu thêm cho phần
# còn lại (chưa chắc đã cần, xem "Ý NGHĨA CON SỐ" ở check_capacity.sh).
#
# CHƯA xác nhận số liệu mặc định thật của waypoint (không có cluster để đo) — dùng tạm mức
# mặc định chung `global.proxy.resources` của Istio (100m CPU / 128Mi RAM request — cùng
# mức đã xác nhận cho sidecar/gateway ở các bản Istio trước, waypoint dùng chung image
# istio-proxy nên nhiều khả năng thừa hưởng cùng mức, nhưng CHƯA gọi thử để đo request thật
# của Deployment "waypoint" sinh ra trên cluster của bạn — kiểm tra bằng:
#   kubectl get deploy waypoint -n <trial_id> -o jsonpath='{.spec.template.spec.containers[0].resources}'
# TRƯỚC khi chạy script này, để biết có thật sự cần hạ hay không.
#
# Dùng: ./05b_trim_waypoint_resources.sh <trial_id>
set -euo pipefail
export PATH="$HOME/bin:$PATH"

TRIAL_ID="${1:?Cần truyền trial_id, vd: ./05b_trim_waypoint_resources.sh trial-01}"

# Mức trim đề xuất — phù hợp traffic RẤT nhẹ (harness + Online Boutique demo) trong PoC
# này. LIMIT không đổi (giữ mặc định Istio) — traffic thật không bị giới hạn thêm, chỉ hạ
# mức "đặt chỗ trước" cho scheduler.
WAYPOINT_CPU_REQUEST="20m"
WAYPOINT_MEM_REQUEST="64Mi"

echo "=== Hạ REQUEST của Deployment 'waypoint' trong namespace '$TRIAL_ID' xuống ${WAYPOINT_CPU_REQUEST}/${WAYPOINT_MEM_REQUEST} ==="

if ! kubectl get deployment waypoint -n "$TRIAL_ID" >/dev/null 2>&1; then
  echo "LỖI: không thấy Deployment 'waypoint' trong namespace '$TRIAL_ID' -- đã chạy" >&2
  echo "     'istioctl waypoint apply -n $TRIAL_ID --enroll-namespace' (bước [2/6] của" >&2
  echo "     05_deploy_trial.sh) chưa?" >&2
  exit 1
fi

kubectl patch deployment waypoint -n "$TRIAL_ID" --type=json -p "$(cat <<'EOF'
[
  {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/cpu", "value": "20m"},
  {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "64Mi"}
]
EOF
)"

echo ""
echo "=== Đợi rollout xong ==="
kubectl rollout status deployment/waypoint -n "$TRIAL_ID" --timeout=180s || \
  echo "  !!! waypoint chưa Ready sau 180s -- kiểm tra 'kubectl get pods -n $TRIAL_ID -l gateway.istio.io/managed'."

echo ""
echo "=> Xong. Kiểm tra request mới:"
echo "   kubectl get deploy waypoint -n $TRIAL_ID -o jsonpath='{.spec.template.spec.containers[0].resources}'"
