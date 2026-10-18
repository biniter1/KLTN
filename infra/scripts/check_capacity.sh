#!/usr/bin/env bash
# So sánh tài nguyên THẬT đang có (Docker Desktop/WSL2, hoặc cluster kind nếu đã tạo) với
# tài nguyên REQUEST mà N trial chạy ĐỒNG THỜI sẽ cần -- trả lời thẳng "chạy nổi không".
#
# Số liệu request/trial lấy THẬT từ manifest gốc Online Boutique (đối chiếu trực tiếp
# release/kubernetes-manifests.yaml trên GitHub GoogleCloudPlatform/microservices-demo,
# nhánh main) + mô hình mesh AMBIENT (ztunnel + waypoint, xem 03_install_istio.sh) --
# KHÔNG còn tính theo sidecar per-pod nữa (bản trước tính the mỗi pod 1 sidecar, đã lỗi
# thời, xem "Vì sao đổi model" bên dưới).
#
# Đây là REQUEST (mức k8s scheduler bắt buộc phải có chỗ mới xếp lịch được), KHÔNG PHẢI
# usage thật lúc chạy (thường thấp hơn nhiều vì đây là traffic demo/harness nhẹ, không
# phải load thật) -- xem phần "Ý NGHĨA CON SỐ" cuối file.
#
# === Vì sao đổi model (sidecar per-pod -> ambient ztunnel+waypoint) ===
# Sidecar: N_TRIALS x N_PODS_TRIAL sidecar -- TUYẾN TÍNH theo tổng số pod (15 trial x 18
# pod/trial = 270 sidecar, riêng phần mesh đã ~27 vCPU/34 GiB mặc định).
# Ambient: ztunnel là DaemonSet -- CHỈ 1 pod/NODE cho CẢ cluster (không nhân theo trial/pod);
# waypoint là 1 Deployment/NAMESPACE (ở đây = 1/trial, vì mỗi trial vẫn 1 namespace riêng
# theo thiết kế hiện tại) -- nhân theo SỐ TRIAL, không phải theo SỐ POD trong trial. Kết
# quả: phần mesh không còn phụ thuộc "mỗi trial có bao nhiêu pod" nữa.
#
# Dùng: ./check_capacity.sh [n_trials] [--trim-waypoint] [--shared]
#   --trim-waypoint : tính theo mức ĐÃ hạ request waypoint (sau khi chạy
#                     05b_trim_waypoint_resources.sh) thay vì mặc định ước tính.
#   --shared        : tính cho kiến trúc "dùng chung hạ tầng" (05_deploy_shared.sh) thay
#                     vì model namespace-riêng (05_deploy_trial.sh x N) -- xem nhánh riêng
#                     phía dưới, khác hẳn phép tính "x N_TRIALS" của model cũ.
set -uo pipefail
export PATH="$HOME/bin:$PATH"

N_TRIALS=15
TRIM_WAYPOINT=0
SHARED=0
for arg in "$@"; do
  case "$arg" in
    --trim-waypoint) TRIM_WAYPOINT=1 ;;
    --trimmed) TRIM_WAYPOINT=1 ;;  # tên cũ, giữ tương thích ngược
    --shared) SHARED=1 ;;
    *) N_TRIALS="$arg" ;;
  esac
done

# --- Request/trial (mili-CPU, Mi RAM) -- xem chú thích nguồn số liệu ở trên ---
APP_CPU_M=1570     # 11 service Online Boutique + loadgenerator (12 pod, container app) -- KHÔNG đổi, không liên quan mesh mode
APP_MEM_MI=1368

# ztunnel: CỐ ĐỊNH theo số NODE của cluster (không theo N_TRIALS) -- số liệu request mặc
# định 200m CPU / 512Mi RAM/pod XÁC NHẬN THẬT từ Helm chart chính thức
# (istio/istio manifests/charts/ztunnel/values.yaml, mục _internal_defaults_do_not_set).
ZTUNNEL_NODE_COUNT=3   # theo manifests/kind-cluster-config.yaml (1 control-plane + 2 worker)
                       # CHƯA xác nhận ztunnel có thật sự chạy trên CẢ 3 node (kể cả control-plane
                       # có taint) trên cluster kind cụ thể này -- ước tính TRÊN, kiểm tra thật bằng
                       # 'kubectl get pods -n istio-system -l app=ztunnel' sau 03_install_istio.sh.
ZTUNNEL_CPU_M=200
ZTUNNEL_MEM_MI=512

# waypoint: 1 Deployment/NAMESPACE (= 1/trial ở model hiện tại) -- số liệu CHƯA xác nhận
# thật (không có cluster để đo), dùng tạm mức mặc định chung của Istio proxy
# (global.proxy.resources, cùng mức đã dùng cho sidecar ở các bản trước Istio) làm ước tính
# tốt nhất hiện có. Kiểm tra thật bằng:
#   kubectl get deploy waypoint -n <trial-id> -o jsonpath='{.spec.template.spec.containers[0].resources}'
if [ "$TRIM_WAYPOINT" -eq 1 ]; then
  WAYPOINT_CPU_M=20     # khớp 05b_trim_waypoint_resources.sh
  WAYPOINT_MEM_MI=64
  echo "(Tính theo mức ĐÃ TRIM waypoint -- xem 05b_trim_waypoint_resources.sh)"
else
  WAYPOINT_CPU_M=100    # ước tính (global.proxy.resources mặc định Istio), CHƯA đo thật
  WAYPOINT_MEM_MI=128
fi

if [ "$SHARED" -eq 1 ]; then
  # --- Kiến trúc "dùng chung hạ tầng" -- app + waypoint chỉ tính 1 LẦN (KHÔNG nhân theo
  # N_TRIALS), vì đó chính là điểm khác biệt duy nhất so với model namespace-riêng (xem
  # scenario_pool.generate_shared_trials/05_deploy_shared.sh). Harness Deployment (90 cạnh,
  # thực tế ~83 sau khử trùng lặp với N_TRIALS=15) KHÔNG khai resources.requests trong
  # render_manifests.py::_harness_deployment_yaml -- xác nhận qua chính source (không có
  # khối "resources:" nào) -- nên đóng góp ~0 vào REQUEST (dù usage THẬT > 0, xem "Ý NGHĨA
  # CON SỐ" cuối file: request khác usage).
  ZTUNNEL_TOTAL_CPU_M=$(( ZTUNNEL_NODE_COUNT * ZTUNNEL_CPU_M ))
  ZTUNNEL_TOTAL_MEM_MI=$(( ZTUNNEL_NODE_COUNT * ZTUNNEL_MEM_MI ))
  NEEDED_CPU_M=$(( APP_CPU_M + WAYPOINT_CPU_M + ZTUNNEL_TOTAL_CPU_M ))
  NEEDED_MEM_MI=$(( APP_MEM_MI + WAYPOINT_MEM_MI + ZTUNNEL_TOTAL_MEM_MI ))

  echo "=== Ước tính REQUEST cần cho kiến trúc DÙNG CHUNG hạ tầng ($N_TRIALS virtual trial, mesh = ambient) ==="
  awk -v c="$APP_CPU_M" -v m="$APP_MEM_MI" 'BEGIN{printf "  Online Boutique thật (1 LẦN, không nhân theo virtual trial): %.2f vCPU, %.2f GiB\n", c/1000, m/1024}'
  awk -v c="$WAYPOINT_CPU_M" -v m="$WAYPOINT_MEM_MI" 'BEGIN{printf "  waypoint (1 LẦN, chung cho toàn bộ namespace):              %.2f vCPU, %.2f GiB\n", c/1000, m/1024}'
  awk -v c="$ZTUNNEL_TOTAL_CPU_M" -v m="$ZTUNNEL_TOTAL_MEM_MI" -v n="$ZTUNNEL_NODE_COUNT" 'BEGIN{printf "  ztunnel (CỐ ĐỊNH, %d node):                                   %.2f vCPU, %.2f GiB\n", n, c/1000, m/1024}'
  echo "  ~90 harness Deployment (không khai resources.requests):      ~0 vCPU, ~0 GiB (request -- KHÔNG phải usage thật)"
  awk -v c="$NEEDED_CPU_M" -v m="$NEEDED_MEM_MI" 'BEGIN{printf "  TỔNG:                                                        %.2f vCPU, %.2f GiB (request)\n", c/1000, m/1024}'
  echo ""
  echo "=== Tài nguyên thực tế phát hiện được ==="
  DOCKER_CPU=""
  DOCKER_MEM_BYTES=""
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER_CPU=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo "")
    DOCKER_MEM_BYTES=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo "")
  fi
  if [ -n "$DOCKER_CPU" ] && [ -n "$DOCKER_MEM_BYTES" ]; then
    DOCKER_MEM_GIB=$(awk -v b="$DOCKER_MEM_BYTES" 'BEGIN{printf "%.2f", b/1024/1024/1024}')
    echo "  Docker Desktop cấp cho VM:  $DOCKER_CPU vCPU, ${DOCKER_MEM_GIB} GiB RAM"
    DOCKER_CPU_M=$(( DOCKER_CPU * 1000 ))
    DOCKER_MEM_MI=$(awk -v b="$DOCKER_MEM_BYTES" 'BEGIN{printf "%.0f", b/1024/1024}')
    echo ""
    echo "=== Kết luận ==="
    if [ "$NEEDED_CPU_M" -le "$DOCKER_CPU_M" ] && [ "$NEEDED_MEM_MI" -le "$DOCKER_MEM_MI" ]; then
      echo "  OK -- đủ chỗ cho CẢ $N_TRIALS virtual trial cùng lúc (dựa trên REQUEST). Đây chính là"
      echo "  điểm khác biệt cốt lõi so với model namespace-riêng: chi phí KHÔNG còn nhân theo"
      echo "  số trial nữa, nên dư SÂU so với trần Docker Desktop thay vì thiếu."
    else
      echo "  !!! VẪN không đủ chỗ dù đã dùng chung hạ tầng -- kiểm tra lại Docker Desktop có"
      echo "      đang cấp đúng tài nguyên như .wslconfig yêu cầu không (mục Sizing trong README)."
    fi
  else
    echo "  !!! Không đọc được thông tin Docker (chưa cài/chưa chạy Docker Desktop?)."
  fi
  echo ""
  echo "Dùng: ./05_deploy_shared.sh [namespace] [n_trials] [base_seed] để deploy theo kiến trúc này."
  exit 0
fi

TRIAL_CPU_M=$(( APP_CPU_M + WAYPOINT_CPU_M ))
TRIAL_MEM_MI=$(( APP_MEM_MI + WAYPOINT_MEM_MI ))
ZTUNNEL_TOTAL_CPU_M=$(( ZTUNNEL_NODE_COUNT * ZTUNNEL_CPU_M ))
ZTUNNEL_TOTAL_MEM_MI=$(( ZTUNNEL_NODE_COUNT * ZTUNNEL_MEM_MI ))
NEEDED_CPU_M=$(( TRIAL_CPU_M * N_TRIALS + ZTUNNEL_TOTAL_CPU_M ))
NEEDED_MEM_MI=$(( TRIAL_MEM_MI * N_TRIALS + ZTUNNEL_TOTAL_MEM_MI ))

echo "=== Ước tính REQUEST cần cho $N_TRIALS trial chạy ĐỒNG THỜI (mesh = ambient) ==="
awk -v c="$TRIAL_CPU_M" -v m="$TRIAL_MEM_MI" 'BEGIN{printf "  Mỗi trial (app + 1 waypoint):  %.2f vCPU, %.2f GiB (request)\n", c/1000, m/1024}'
awk -v c="$ZTUNNEL_TOTAL_CPU_M" -v m="$ZTUNNEL_TOTAL_MEM_MI" -v n="$ZTUNNEL_NODE_COUNT" 'BEGIN{printf "  ztunnel (CỐ ĐỊNH, %d node, không tăng theo trial): %.2f vCPU, %.2f GiB\n", n, c/1000, m/1024}'
awk -v c="$NEEDED_CPU_M" -v m="$NEEDED_MEM_MI" -v n="$N_TRIALS" 'BEGIN{printf "  Tổng %2d trial + ztunnel:  %.2f vCPU, %.2f GiB (request)\n", n, c/1000, m/1024}'
echo ""

# --- Tài nguyên thực tế đang có ---
echo "=== Tài nguyên thực tế phát hiện được ==="

DOCKER_CPU=""
DOCKER_MEM_BYTES=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  DOCKER_CPU=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo "")
  DOCKER_MEM_BYTES=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo "")
fi

if [ -n "$DOCKER_CPU" ] && [ -n "$DOCKER_MEM_BYTES" ]; then
  DOCKER_MEM_GIB=$(awk -v b="$DOCKER_MEM_BYTES" 'BEGIN{printf "%.2f", b/1024/1024/1024}')
  echo "  Docker Desktop cấp cho VM:  $DOCKER_CPU vCPU, ${DOCKER_MEM_GIB} GiB RAM"
  echo "  (đây là TRẦN thật -- mọi container kind/K8s đều chạy TRONG giới hạn này, dù"
  echo "   .wslconfig có ghi số lớn hơn cũng không vượt qua được trần Docker Desktop)"
else
  echo "  !!! Không đọc được thông tin Docker (chưa cài/chưa chạy Docker Desktop?)."
fi

if command -v kubectl >/dev/null 2>&1 && kubectl cluster-info >/dev/null 2>&1; then
  echo ""
  echo "  Cluster kind đang chạy -- đọc allocatable THẬT từ từng node:"
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.cpu}{"\t"}{.status.allocatable.memory}{"\n"}{end}' | \
    while IFS=$'\t' read -r name cpu mem; do
      echo "    - $name: cpu=$cpu, memory=$mem"
    done
  ALLOC_CPU_TOTAL=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.cpu}{"\n"}{end}' | \
    sed 's/m$//' | awk '{ if ($1+0==$1) s+=$1; else s+=$1*1000 } END{print s+0}')
  echo "    Tổng allocatable CPU toàn cluster: ${ALLOC_CPU_TOTAL}m (~$(awk -v c="$ALLOC_CPU_TOTAL" 'BEGIN{printf "%.2f", c/1000}') vCPU)"
  ZTUNNEL_ACTUAL=$(kubectl get pods -n istio-system -l app=ztunnel --no-headers 2>/dev/null | wc -l)
  if [ "$ZTUNNEL_ACTUAL" -gt 0 ] && [ "$ZTUNNEL_ACTUAL" -ne "$ZTUNNEL_NODE_COUNT" ]; then
    echo "    (Lưu ý: script giả định $ZTUNNEL_NODE_COUNT ztunnel pod, nhưng cluster thật đang"
    echo "     có $ZTUNNEL_ACTUAL -- sửa ZTUNNEL_NODE_COUNT trong script này cho khớp nếu muốn"
    echo "     số ước tính chính xác hơn.)"
  fi
else
  echo ""
  echo "  (Chưa có cluster kind đang chạy -- chỉ so sánh được với trần Docker Desktop ở trên;"
  echo "   chạy lại script này SAU 02_create_cluster.sh để xem allocatable thật từng node.)"
fi

echo ""
echo "=== Kết luận ==="
if [ -n "$DOCKER_CPU" ] && [ -n "$DOCKER_MEM_BYTES" ]; then
  DOCKER_CPU_M=$(( DOCKER_CPU * 1000 ))
  DOCKER_MEM_MI=$(awk -v b="$DOCKER_MEM_BYTES" 'BEGIN{printf "%.0f", b/1024/1024}')
  AVAILABLE_FOR_TRIALS_CPU_M=$(( DOCKER_CPU_M - ZTUNNEL_TOTAL_CPU_M ))
  AVAILABLE_FOR_TRIALS_MEM_MI=$(( DOCKER_MEM_MI - ZTUNNEL_TOTAL_MEM_MI ))
  MAX_TRIALS_CPU=$(( AVAILABLE_FOR_TRIALS_CPU_M / TRIAL_CPU_M ))
  MAX_TRIALS_MEM=$(( AVAILABLE_FOR_TRIALS_MEM_MI / TRIAL_MEM_MI ))
  MAX_TRIALS=$(( MAX_TRIALS_CPU < MAX_TRIALS_MEM ? MAX_TRIALS_CPU : MAX_TRIALS_MEM ))
  echo "  Với tài nguyên Docker Desktop hiện tại, ước tính CHẠY ĐỒNG THỜI ĐƯỢC TỐI ĐA: $MAX_TRIALS trial"
  echo "  (giới hạn bởi $([ "$MAX_TRIALS_CPU" -lt "$MAX_TRIALS_MEM" ] && echo CPU || echo RAM), sau khi trừ ztunnel cố định)"
  if [ "$MAX_TRIALS" -lt "$N_TRIALS" ]; then
    echo ""
    echo "  !!! KHÔNG đủ để chạy $N_TRIALS trial cùng lúc theo model NAMESPACE RIÊNG hiện tại."
    echo "      Lưu ý quan trọng: phần LỚN chi phí giờ là APP CONTAINER (Online Boutique thật x"
    echo "      $N_TRIALS bản sao), KHÔNG còn là mesh proxy nữa (ambient đã cắt gần hết phần đó)."
    echo "      Vì vậy hướng xử lý hiệu quả nhất bây giờ KHÔNG phải trim proxy thêm, mà là:"
    echo "      1. Dùng kiến trúc DÙNG CHUNG hạ tầng (1 Online Boutique thật + ~83 harness"
    echo "         Deployment chia sẻ theo pair_key(), KHÔNG dùng injection_id -- xem"
    echo "         scenario_pool.generate_shared_trials/05_deploy_shared.sh) -- app chỉ chạy"
    echo "         1 LẦN thay vì x$N_TRIALS, cắt đúng phần chi phí đang chiếm ưu thế. ĐÃ"
    echo "         IMPLEMENT XONG -- chạy thử: './check_capacity.sh $N_TRIALS --shared' rồi"
    echo "         deploy bằng './05_deploy_shared.sh'. Đây là hướng ĐƯỢC KHUYẾN NGHỊ."
    echo "      2. Batching (--batch-size $MAX_TRIALS ở 06_deploy_all_trials.sh) -- ĐÁNH ĐỔI:"
    echo "         các batch không còn chung 1 cửa sổ 7 ngày nữa."
    echo "      3. Giảm resources.requests trong manifest Online Boutique trước khi apply --"
    echo "         số request gốc (vd cartservice 200m/64Mi) là mức AN TOÀN cho demo GKE thật,"
    echo "         KHÔNG phải mức tối thiểu."
    echo "      4. Chạy trên máy/VM lớn hơn (cloud) cho riêng cửa sổ 7 ngày thu dữ liệu."
  else
    echo "  OK -- ước tính đủ chỗ cho $N_TRIALS trial (dựa trên REQUEST, chưa tính overhead"
    echo "  hệ thống/kubelet -- vẫn nên theo dõi 'kubectl get pods -A | grep -v Running' sau"
    echo "  khi deploy thật, xem 07_verify.sh)."
  fi
fi

cat <<'EOF'

=== Ý NGHĨA CON SỐ (đọc trước khi quyết định) ===
Con số trên là REQUEST (chỗ đặt trước bắt buộc để scheduler xếp lịch), không phải usage
THẬT lúc chạy. K8s sẽ từ chối xếp lịch (Pending) nếu tổng request vượt allocatable, BẤT KỂ
usage thật có thấp cỡ nào -- đây là lý do phải kiểm tra request trước, không thể "cứ chạy
thử xem có OOM không". Ngược lại, usage THẬT (RAM/CPU container thực sự dùng) thường thấp
hơn request nhiều với traffic nhẹ như harness ở đây tạo ra.

Khác biệt quan trọng so với bản sidecar cũ: dưới ambient, phần "linh hoạt để trim" (mesh
proxy) đã nhỏ đi rất nhiều và gần như KHÔNG còn tăng theo số trial nữa (ztunnel cố định,
waypoint chỉ 1/namespace). Vì vậy nếu vẫn thiếu chỗ, trim thêm waypoint (mục 3 ở trên, script
05b_trim_waypoint_resources.sh) sẽ KHÔNG giải quyết được nhiều -- phần chiếm tài nguyên lớn
nhất bây giờ là chính APP CONTAINER (Online Boutique thật), nhân theo số trial. Đây là đúng
lý do kiến trúc "dùng chung hạ tầng" (mục 1 ở trên) mới thật sự giải quyết được vấn đề, không
phải việc đổi mesh mode (dù đổi mesh mode vẫn đáng làm, vì nó cắt phần lớn chi phí PHỤ, chỉ
là không đủ MỘT MÌNH).
EOF
