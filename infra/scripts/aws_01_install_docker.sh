#!/usr/bin/env bash
# Cài Docker Engine trên EC2 Ubuntu THẲNG (không phải Docker Desktop/WSL2) -- chạy 1 lần
# NGAY SAU KHI SSH vào instance mới, TRƯỚC 00_check_prereqs.sh. Theo đúng hướng dẫn cài
# chính thức của Docker cho Ubuntu (apt repo chính thức download.docker.com).
set -euo pipefail

echo "=== Gỡ bản cũ (nếu có) từ apt mặc định của Ubuntu (thường xung đột) ==="
for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do
  sudo apt-get remove -y "$pkg" 2>/dev/null || true
done

echo "=== Cài Docker Engine từ repo chính thức Docker ==="
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "=== Cho phép user hiện tại dùng docker không cần sudo ==="
sudo usermod -aG docker "$USER"

echo ""
echo "=> Cài xong. Chạy 'newgrp docker' HOẶC đăng xuất/SSH lại để nhóm 'docker' có hiệu"
echo "   lực, rồi kiểm tra: docker run hello-world"
echo "   Sau đó chạy tiếp scripts/00_check_prereqs.sh như bình thường (bỏ qua các dòng"
echo "   nhắc 'Docker Desktop'/'WSL2' trong thông báo — chỉ là chữ, không ảnh hưởng chức năng"
echo "   vì bên dưới vẫn chỉ gọi 'docker info'/'nproc'/'free' như trên Linux thường)."
