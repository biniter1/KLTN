#!/usr/bin/env bash
# Cài kubectl, kind, istioctl vào ~/bin (không cần sudo cho phần binary, chỉ cần sudo để
# di chuyển vào /usr/local/bin nếu muốn dùng toàn hệ thống — ở đây dùng ~/bin cho gọn).
# Chạy TRONG WSL2 (Ubuntu). Idempotent — chạy lại không hỏng gì.
set -euo pipefail

mkdir -p "$HOME/bin"
export PATH="$HOME/bin:$PATH"

echo "=== kubectl ==="
if command -v kubectl >/dev/null 2>&1; then
    echo "  đã có: $(kubectl version --client --short 2>/dev/null || true)"
else
    KUBECTL_VERSION=$(curl -sL https://dl.k8s.io/release/stable.txt)
    curl -sLo "$HOME/bin/kubectl" "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
    chmod +x "$HOME/bin/kubectl"
    echo "  cài xong: ${KUBECTL_VERSION}"
fi

echo "=== kind ==="
# CẦN >=v0.31.0 -- kind-cluster-config.yaml ghim node image Kubernetes v1.31.14 (xem comment
# trong file đó: Gateway API v1.6.0 dùng hàm CEL isIP()/format.dns1123Label() chỉ có từ
# Kubernetes >=1.31, phát hiện thật lúc chạy trên máy Bin 2026-09-27 với kind v0.23.0 cũ mặc
# định ra Kubernetes v1.30.0 -> apply CRD lỗi ngay). v0.31.0 là bản kind CHÍNH THỨC công bố đã
# test cùng node image v1.31.14 (xem release notes kind), nên KHÔNG chỉ cần ">=1.31" chung
# chung mà cần đúng cặp kind-CLI/node-image này để kind tự bootstrap không lỗi.
MIN_KIND_VERSION="v0.31.0"
CURRENT_KIND_VERSION=""
if command -v kind >/dev/null 2>&1; then
    CURRENT_KIND_VERSION=$(kind version | awk '{print $2}')
fi
if [ -n "$CURRENT_KIND_VERSION" ] && [ "$(printf '%s\n%s\n' "$MIN_KIND_VERSION" "$CURRENT_KIND_VERSION" | sort -V | head -1)" = "$MIN_KIND_VERSION" ]; then
    echo "  đã có, đủ mới: $(kind version) (>= $MIN_KIND_VERSION)"
else
    if [ -n "$CURRENT_KIND_VERSION" ]; then
        echo "  !!! kind hiện có ($CURRENT_KIND_VERSION) CŨ HƠN $MIN_KIND_VERSION yêu cầu -- tải bản mới vào ~/bin,"
        echo "      NHƯNG nếu máy bạn có kind khác nằm TRƯỚC \$HOME/bin trong PATH (vd /usr/local/bin/kind,"
        echo "      dùng 'which kind' để kiểm tra) thì lệnh 'kind' sau này vẫn chạy bản cũ đó -- cần tự"
        echo "      ghi đè/gỡ bản cũ đó (vd: sudo cp \"\$HOME/bin/kind\" /usr/local/bin/kind) hoặc sửa PATH."
    fi
    curl -sLo "$HOME/bin/kind" "https://github.com/kubernetes-sigs/kind/releases/download/${MIN_KIND_VERSION}/kind-linux-amd64"
    chmod +x "$HOME/bin/kind"
    echo "  cài xong: kind ${MIN_KIND_VERSION} vào \$HOME/bin/kind"
fi

echo "=== istioctl (dùng script cài chính thức — tự chọn bản mới nhất, tránh hardcode version cũ) ==="
if command -v istioctl >/dev/null 2>&1; then
    echo "  đã có: $(istioctl version --remote=false 2>/dev/null || true)"
else
    TMP_DIR=$(mktemp -d)
    (cd "$TMP_DIR" && curl -sL https://istio.io/downloadIstio | sh -)
    ISTIO_DIR=$(find "$TMP_DIR" -maxdepth 1 -type d -name 'istio-*' | head -1)
    cp "$ISTIO_DIR/bin/istioctl" "$HOME/bin/istioctl"
    chmod +x "$HOME/bin/istioctl"
    # Giữ lại thư mục cài (có samples/addons/ cần cho bước 03) — copy vào chỗ cố định
    mkdir -p "$HOME/.local/share/istio"
    cp -r "$ISTIO_DIR" "$HOME/.local/share/istio/current"
    rm -rf "$TMP_DIR"
    echo "  cài xong, bản: $("$HOME/bin/istioctl" version --remote=false)"
    echo "  thư mục samples/addons (Prometheus...): $HOME/.local/share/istio/current/samples/addons"
fi

echo ""
echo "=== Thêm ~/bin vào PATH lâu dài (nếu chưa) ==="
if ! grep -q 'HOME/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/bin:$PATH"' >> "$HOME/.bashrc"
    echo "  đã thêm vào ~/.bashrc — mở terminal mới hoặc 'source ~/.bashrc' để có hiệu lực."
else
    echo "  ~/.bashrc đã có sẵn rồi."
fi

echo ""
echo "=== Kiểm tra lại ==="
export PATH="$HOME/bin:$PATH"
kubectl version --client --short 2>/dev/null || kubectl version --client
kind version
istioctl version --remote=false
