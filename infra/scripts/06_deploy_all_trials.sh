#!/usr/bin/env bash
# Deploy CẢ BỘ R=15 trial theo model NAMESPACE RIÊNG (1 trial = 1 namespace = 1 bản Online
# Boutique thật) — gọi lặp 05_deploy_trial.sh cho từng trial.
#
# !!! ĐÃ XÁC NHẬN THẬT (check_capacity.sh, 2026-09-27, máy Bin): model này chỉ chạy nổi
# TỐI ĐA 4/15 trial đồng thời trên laptop thật (giới hạn RAM — phần chiếm tài nguyên lớn
# nhất là APP CONTAINER Online Boutique thật x15 bản, không phải mesh proxy). Nếu máy bạn
# rơi vào tình huống tương tự (`check_capacity.sh 15` báo không đủ), dùng
# `05_deploy_shared.sh` THAY CHO script này — kiến trúc "dùng chung hạ tầng" (1 Online
# Boutique thật + N virtual trial CHIA SẺ namespace, xem `scenario_pool.generate_shared_trials`)
# hạ tổng chi phí REQUEST từ ~25.65 vCPU/23.41 GiB xuống ~2.27 vCPU/2.96 GiB cho CẢ 15 trial
# (chạy thử: `check_capacity.sh 15 --shared`). Script NÀY (namespace riêng) vẫn được giữ lại
# cho trường hợp chạy trên máy/VM đủ lớn, hoặc muốn cách ly hoàn toàn từng trial.
#
# QUAN TRỌNG (đánh đổi đã chọn cùng người dùng): mục tiêu ban đầu là chạy CẢ 15 trial
# ĐỒNG THỜI, quan sát chung 1 cửa sổ 7 ngày thật duy nhất (thay vì R=15 lần x 7 ngày =
# ~105 ngày nếu chạy tuần tự) -- xem PROPOSAL.md mục 7. Script này vì vậy mặc định deploy
# CẢ 15 TRIAL CÙNG LÚC. Nếu laptop không đủ tài nguyên (pod Pending/OOMKilled), dùng
# --batch-size để chia nhỏ -- NHƯNG mỗi batch tách rời NHAU VỀ CỬA SỔ QUAN SÁT: batch 2
# sẽ có "ngày 0" muộn hơn batch 1, nghĩa là kết quả R=15 không còn cùng 1 cửa sổ 7 ngày
# nữa (không sai về mặt thống kê -- mỗi trial vẫn độc lập -- nhưng làm mất lý do ban đầu
# để chạy song song). Cân nhắc kỹ trước khi batching — hoặc dùng 05_deploy_shared.sh, vừa
# đủ tài nguyên VỪA giữ được đúng 1 cửa sổ 7 ngày chung cho cả 15 virtual trial.
#
# Dùng: ./06_deploy_all_trials.sh [--batch-size N] [--n-trials N] [--base-seed N]
set -euo pipefail
export PATH="$HOME/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

N_TRIALS=15
BASE_SEED=20260101
BATCH_SIZE=15  # mặc định: tất cả cùng lúc, đúng thiết kế "1 cửa sổ 7 ngày chung"

while [ $# -gt 0 ]; do
  case "$1" in
    --batch-size) BATCH_SIZE="$2"; shift 2 ;;
    --n-trials) N_TRIALS="$2"; shift 2 ;;
    --base-seed) BASE_SEED="$2"; shift 2 ;;
    *) echo "Tham số không rõ: $1" >&2; exit 1 ;;
  esac
done

if [ "$BATCH_SIZE" -lt "$N_TRIALS" ]; then
  echo "!!! CẢNH BÁO: batch-size ($BATCH_SIZE) < n-trials ($N_TRIALS)."
  echo "    Các trial sẽ KHÔNG cùng bắt đầu 1 cửa sổ 7 ngày -- xem giải thích ở đầu file này."
  read -r -p "    Vẫn tiếp tục? (y/N) " confirm
  [ "$confirm" = "y" ] || exit 1
fi

# trial_id theo đúng định dạng scenario_pool.py::generate_all_trials() -- "trial-01"..
# ("trial_01" với dấu gạch dưới sẽ bị kubectl từ chối làm tên namespace, xem
# render_manifests.py::_validate_k8s_name).
TRIAL_IDS=()
for i in $(seq 1 "$N_TRIALS"); do
  TRIAL_IDS+=("$(printf 'trial-%02d' "$i")")
done

echo "=== Deploy $N_TRIALS trial, batch size = $BATCH_SIZE, base_seed = $BASE_SEED ==="
count=0
pids=()
for i in "${!TRIAL_IDS[@]}"; do
  trial_id="${TRIAL_IDS[$i]}"
  seed=$((BASE_SEED + i + 1))
  echo "--- Deploy $trial_id (seed=$seed) ---"
  "$SCRIPT_DIR/05_deploy_trial.sh" "$trial_id" "$seed" &
  pids+=($!)
  count=$((count + 1))

  if [ "$count" -ge "$BATCH_SIZE" ]; then
    echo ">>> Đợi batch hiện tại ($count trial) deploy xong trước khi tiếp tục..."
    for pid in "${pids[@]}"; do wait "$pid"; done
    pids=()
    count=0
  fi
done
# Đợi phần còn lại (batch cuối, chưa đủ batch-size)
for pid in "${pids[@]}"; do wait "$pid"; done

echo ""
echo "=== Đã gửi yêu cầu deploy toàn bộ $N_TRIALS trial ==="
echo "Theo dõi tài nguyên trong lúc pod khởi động (đặc biệt lần đầu, phải kéo image):"
echo "  kubectl top nodes            # nếu metrics-server có sẵn"
echo "  docker stats                 # xem trực tiếp CPU/RAM container trên Docker Desktop"
echo "  kubectl get pods -A | grep -v Running   # tìm pod chưa Running/bị Pending"
echo ""
echo "Nếu thấy nhiều pod Pending kéo dài (không phải đang Pull image) hoặc bị OOMKilled,"
echo "laptop không đủ tài nguyên để chạy $N_TRIALS trial x ~11 service cùng lúc -- cân nhắc"
echo "chạy lại với --batch-size nhỏ hơn (chấp nhận đánh đổi nêu ở đầu file), hoặc chuyển"
echo "sang máy/VM lớn hơn."
echo ""
echo "Sau khi TẤT CẢ pod Running & sẵn sàng, chạy: ./07_verify.sh để xác nhận ztunnel/waypoint/"
echo "nhãn trước khi tính 'ngày 0' của cửa sổ quan sát 7 ngày."
