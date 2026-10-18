#!/usr/bin/env bash
# Cài Istio ở chế độ AMBIENT (ztunnel + waypoint), KHÔNG dùng sidecar, + Prometheus addon.
# Chạy TRONG WSL2, sau khi cluster đã tạo (02_create_cluster.sh).
#
# VÌ SAO AMBIENT THAY VÌ SIDECAR (quyết định đổi ngày 2026-09-26, thay cho bản trước dùng
# `profile=demo` + sidecar injection):
#   - Sidecar: mỗi pod tự mang 1 Envoy riêng -> chi phí request TĂNG TUYẾN TÍNH theo số
#     pod (15 trial x ~18 pod/trial = 270 sidecar, ~27 vCPU/34 GiB chỉ riêng phần mesh —
#     đây là lý do bản trước phải viết 05b_trim_sidecar_resources.sh để hạ request).
#   - Ambient: `ztunnel` chạy dạng DaemonSet — CHỈ 1 pod/node cho CẢ cluster (không tăng
#     theo số pod/trial); phần xử lý L7 (cần cho `istio_requests_total`/HEALTHCHECK
#     classification, xem TRAFFIC_COLLECTION_PIPELINE.md) tách riêng ra `waypoint` proxy
#     dùng CHUNG cho cả 1 namespace, không phải 1:1 theo pod. Kết quả: chi phí mesh gần
#     như CỐ ĐỊNH dù có bao nhiêu harness pod, thay vì nhân theo pod như sidecar.
#   - Đánh đổi PHẢI biết: ambient THUẦN (chỉ ztunnel, không có waypoint) CHỈ sinh metric L4
#     (`istio_tcp_*`) — KHÔNG sinh `istio_requests_total` (L7, HTTP/gRPC) vì ztunnel không
#     parse HTTP. Toàn bộ rule phân loại HEALTHCHECK (path /health*...) và cột "HTTP/gRPC"
#     trong bảng TRAFFIC_COLLECTION_PIPELINE.md mục 1 PHỤ THUỘC `istio_requests_total` ->
#     BẮT BUỘC phải dùng ambient + waypoint (không phải ambient thuần), xem 05_deploy_trial.sh.
#   - Danh tính/identity (canonical-name label + serviceAccountName) và cơ chế mTLS/SPIFFE
#     KHÔNG đổi giữa sidecar và ambient — cả 2 đều dùng chung CA/identity model của Istio,
#     nên Identity Resolution ở TIER1_DESIGN.md mục 3 không bị ảnh hưởng.
#
# ĐÃ XÁC NHẬN THẬT (chạy trên máy Bin, 2026-09-27): bước cài Gateway API CRD ngay dưới đây
# (bắt buộc để tạo waypoint) THẤT BẠI nếu cluster kind chạy Kubernetes <1.32 -- CRD `TLSRoute`
# dùng CEL `isIP()` (cần >=1.30) và CRD `XBackend` dùng CEL `format.dns1123Label()` (cần
# >=1.31 CHỨ KHÔNG PHẢI SỐ TRÒN >=1.31 THEO NGHĨA "cluster chạy 1.31 là đủ" -- xem lý do "n-1"
# ở comment chi tiết trong manifests/kind-cluster-config.yaml, tra tận gốc source Kubernetes).
# Tóm tắt: môi trường CEL dùng để validate CRD mặc định lùi lại (n-1) so với version cluster
# thật đang chạy, nên cluster phải chạy >=1.32 (không phải >=1.31) thì `format.dns1123Label()`
# mới thật sự được bật. ĐÃ SỬA bằng cách ghim cứng node image `kindest/node:v1.32.11` trong
# manifests/kind-cluster-config.yaml (đọc comment ở đó để hiểu rõ + biết cách tự tra nếu gặp
# lỗi CEL tương tự với hàm khác sau này) + yêu cầu kind CLI >=v0.31.0 (01_install_tools.sh) --
# nếu bạn thấy lại lỗi "undeclared reference to '<tên hàm>'" này, kiểm tra `kubectl get nodes`
# xem VERSION trước khi nghi ngờ chỗ khác.
#
# CHƯA kiểm chứng trên cluster thật (rủi ro KHÁC, chưa gặp phải vì chưa tới bước này lúc phát
# hiện lỗi ở trên): ambient trên `kind` (CNI mặc định kindnet) — Istio CNI plugin chain được
# với hầu hết CNI theo tài liệu chính thức, nhưng tài liệu không nêu rõ kindnet cụ thể; có báo
# cáo lỗi cũ (istio/istio#41058) liên quan cài ambient trên kind (root cause: lỗi lấy host IP,
# không rõ đã hết trên bản Istio mới hay chưa, xem lại `istioctl x precheck` nếu bước dưới
# không lên đủ Ready). Nếu gặp lỗi ở bước này và không tự sửa được, cân nhắc phương án dự
# phòng: quay lại `profile=demo` (sidecar, README bản cũ) cho riêng cụm kind, chấp nhận lại
# chi phí per-pod.
set -euo pipefail
export PATH="$HOME/bin:$PATH"

echo "=== Cài Gateway API CRD (bắt buộc để tạo waypoint — Istio ambient dùng Gateway API) ==="
if ! kubectl get crd gateways.gateway.networking.k8s.io >/dev/null 2>&1; then
  kubectl apply --server-side -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.0/experimental-install.yaml
else
  echo "  đã có CRD Gateway API — bỏ qua."
fi

echo ""
echo "=== istioctl install (profile=ambient) ==="
istioctl install --set profile=ambient --skip-confirmation

echo ""
echo "=== Prometheus addon ==="
ADDON_DIR="$HOME/.local/share/istio/current/samples/addons"
if [ -f "$ADDON_DIR/prometheus.yaml" ]; then
    kubectl apply -f "$ADDON_DIR/prometheus.yaml"
else
    echo "  Không thấy $ADDON_DIR (có thể istioctl được cài cách khác) — tải trực tiếp từ GitHub."
    echo "  Nếu máy không ra được Internet ngoài lúc này, tự tải file 'samples/addons/prometheus.yaml'"
    echo "  từ release Istio bạn đang dùng (https://github.com/istio/istio/releases) rồi 'kubectl apply -f'."
    kubectl apply -f "https://raw.githubusercontent.com/istio/istio/master/samples/addons/prometheus.yaml"
fi

echo ""
echo "=== Chờ pod istio-system sẵn sàng (istiod + ztunnel DaemonSet + istio-cni-node) ==="
kubectl -n istio-system wait --for=condition=Ready pods --all --timeout=180s || true
kubectl get pods -n istio-system

echo ""
echo "=> Nếu mọi pod ở trên đều Running/Ready (đặc biệt 'ztunnel' — DaemonSet, 1 pod/node,"
echo "   và 'istio-cni-node'): Istio ambient + Prometheus đã sẵn sàng."
echo "   Prometheus xem qua: kubectl -n istio-system port-forward svc/prometheus 9090:9090"
echo "   rồi mở http://localhost:9090 (từ trình duyệt Windows vẫn vào được cổng WSL2 forward)."
echo ""
echo "   LƯU Ý: 'profile=ambient' KHÔNG tự tạo waypoint nào — waypoint được tạo RIÊNG cho"
echo "   từng namespace/trial ở bước 05 (istioctl waypoint apply --enroll-namespace), vì"
echo "   namespace trial chưa tồn tại ở bước này."
