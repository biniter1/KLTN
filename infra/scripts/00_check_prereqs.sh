#!/usr/bin/env bash
# Kiểm tra điều kiện tiên quyết trước khi dựng hạ tầng — chạy TRONG WSL2 (Ubuntu).
# Không sửa gì trên máy, chỉ báo cáo.
set -euo pipefail

pass() { echo "  [OK]   $1"; }
fail() { echo "  [FAIL] $1"; FAILED=1; }
warn() { echo "  [WARN] $1"; }

FAILED=0

echo "=== 1. Docker (từ Docker Desktop, tích hợp WSL2) ==="
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    pass "docker hoạt động: $(docker --version)"
else
    fail "không gọi được 'docker info' — mở Docker Desktop trên Windows, vào Settings > Resources > WSL Integration, bật cho distro Ubuntu đang dùng"
fi

echo "=== 2. Tài nguyên WSL2 thấy được ==="
TOTAL_MEM_GB=$(free -g | awk '/^Mem:/{print $2}')
CPU_COUNT=$(nproc)
echo "  RAM WSL2 thấy được : ${TOTAL_MEM_GB} GB"
echo "  CPU WSL2 thấy được : ${CPU_COUNT}"
if [ "$TOTAL_MEM_GB" -lt 8 ]; then
    warn "dưới 8GB RAM — kind + Istio + vài trial Online Boutique cùng lúc sẽ CHẬM/OOM." \
         " Cân nhắc tăng qua %USERPROFILE%\\.wslconfig (mục [wsl2] memory=..., processors=...)" \
         " rồi 'wsl --shutdown' từ PowerShell để áp dụng, restart Docker Desktop."
elif [ "$TOTAL_MEM_GB" -lt 16 ]; then
    warn "8-16GB RAM — chạy được nhưng PARALLEL_TRIALS nên để 2-3, không nên để 15."
else
    pass "đủ RAM để thử PARALLEL_TRIALS ở mức vừa phải (4-6), xem README phần sizing."
fi

echo "=== 3. Dung lượng đĩa (kind image + Online Boutique images khá nặng) ==="
AVAIL_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
echo "  Còn trống: ${AVAIL_GB} GB"
if [ "$AVAIL_GB" -lt 20 ]; then
    warn "dưới 20GB trống — Online Boutique + Istio images có thể chiếm 5-10GB/trial namespace."
else
    pass "đủ dung lượng đĩa cho vài trial."
fi

echo "=== 4. Các lệnh cần cho bước sau (chưa cài cũng không sao, script 01 sẽ cài) ==="
for cmd in kubectl kind istioctl curl python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
        pass "$cmd đã có: $(command -v "$cmd")"
    else
        warn "$cmd CHƯA có — script 01_install_tools.sh sẽ cài (trừ python3, cài qua 'sudo apt install python3')"
    fi
done

echo ""
if [ "$FAILED" -eq 1 ]; then
    echo "=> Có mục FAIL — sửa xong rồi chạy lại script này trước khi qua bước 01."
    exit 1
fi
echo "=> Đủ điều kiện để tiếp tục (đọc kỹ mọi WARN, đặc biệt về RAM)."
