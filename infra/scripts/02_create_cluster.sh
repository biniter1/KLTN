#!/usr/bin/env bash
# Tạo kind cluster cho thí nghiệm. Chạy TRONG WSL2, sau khi 01_install_tools.sh xong.
set -euo pipefail
export PATH="$HOME/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-kltn-eval}"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "Cluster '$CLUSTER_NAME' đã tồn tại — bỏ qua tạo mới."
    echo "(Muốn tạo lại từ đầu: 'kind delete cluster --name $CLUSTER_NAME' rồi chạy lại script này.)"
else
    kind create cluster --name "$CLUSTER_NAME" --config "$SCRIPT_DIR/../manifests/kind-cluster-config.yaml"
fi

kubectl cluster-info --context "kind-${CLUSTER_NAME}"
echo ""
echo "=== Node & maxPods đã áp dụng ==="
kubectl get nodes -o wide
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.pods}{"\n"}{end}'
echo ""
echo "=> Cluster sẵn sàng. kubeconfig đã tự trỏ vào context 'kind-${CLUSTER_NAME}'."
echo "   Kiểm tra: kubectl config current-context"
