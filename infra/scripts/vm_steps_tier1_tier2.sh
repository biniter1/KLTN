# Lệnh chạy trên VM Azure — KHÔNG chạy cả file một lượt. Chạy từng khối, kiểm kết quả rồi mới sang khối sau.
# Giả định: repo clone ở ~/kltn-repo (nhánh tier2-run-tooling = main + công cụ Tier 2), venv ~/venv-kltn đã cài đủ.
# Mỗi terminal mới: source ~/venv-kltn/bin/activate  và  END="2026-10-26T23:59:59Z"
# PHƯƠNG ÁN B: dùng cửa sổ dữ liệu CŨ 20/10–26/10 (END = 2026-10-26T23:59:59Z), không chờ 7 ngày.
# HẠN CHÓT: retention 15d -> dữ liệu 20/10 bị xoá khoảng 04/11 ~00:00–07:00 giờ VN. Chạy xong khối 1 + khối 4 TRƯỚC 03/11 tối.
# KHÔNG restart/sửa deployment prometheus (storage emptyDir: mất pod = mất hết dữ liệu).
END="2026-10-26T23:59:59Z"
# API key/token: bạn tự export trong shell, KHÔNG dán vào chat.

# ===== Khối 0: cập nhật code và kiểm điều kiện =====
cd ~/kltn-repo
git fetch origin && git checkout tier2-run-tooling && git pull --ff-only
git rev-parse HEAD                      # ghi lại hash này
for d in tier1_inference tier2_agent infra orchestration; do (cd $d && echo "$d: $(python3 -m pytest -q -p no:warnings 2>&1 | tail -1)"); done

# Cluster còn chạy, harness còn sinh traffic?
kubectl get nodes
kubectl -n shared-eval get pods --no-headers | wc -l
kubectl -n shared-eval get pods -l test-harness=true -o custom-columns=NAME:.metadata.name,START:.status.startTime | head

# Prometheus: retention và dữ liệu cũ nhất còn giữ
kubectl -n istio-system get deploy prometheus -o yaml | grep -n -- "--storage.tsdb.retention" || echo "không đặt retention => mặc định 15d"
kubectl -n istio-system port-forward svc/prometheus 9090:9090 >/tmp/pf-prom.log 2>&1 &
sleep 3
curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_lowest_timestamp_seconds' | python3 -m json.tool | grep -A1 value
# -> đổi epoch ra ngày: date -d @<số>. Cần dữ liệu liên tục ít nhất 7 ngày tính tới hôm nay.
# Số request từng ngày 20/10..26/10 tại mốc END (cả 7 dòng phải > 0; dòng nào = 0 thì DỪNG, báo mình)
for d in 0 1 2 3 4 5 6; do
  t=$(( $(date -d "$END" +%s) - d*86400 ))
  echo -n "$(date -u -d @$t +%F): "
  curl -s 'http://localhost:9090/api/v1/query' --data-urlencode "query=sum(increase(istio_requests_total{destination_workload_namespace=\"shared-eval\"}[1d] @ $t))" \
    | python3 -c "import json,sys;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)"
done

ls infra/.cache/shared-eval.owner_map.json   # chép từ ~/KLTN/infra/.cache/, phải có

# ===== Khối 1: xuất lại trial data (có port, traffic_days, series_daily_counts) =====
export OPENAI_API_KEY="..."            # Config Collector (gpt-4o-mini)
cd ~/kltn-repo/infra/scripts
python3 08_export_trial_data.py --shared --shared-namespace shared-eval \
  --owner-map ../.cache/shared-eval.owner_map.json --all \
  --prometheus-url http://localhost:9090 \
  --cache-db ../.cache/config_llm_cache.sqlite3 \
  --end-time "$END" \
  --out-dir ../trial_data_v2 2>&1 | tee ../export_v2.log
# Ghi sang trial_data_v2 để KHÔNG đè 15 file 08/10.
python3 -c "import json;d=json.load(open('../trial_data_v2/trial-01.json'));print(sorted(d.keys()))"
# Kiểm có 'series_daily_counts' và series có 'port_source'/'traffic_days'.

# ===== Khối 2: số chính thức Tầng 1 (validator selector THẬT) =====
python3 09_run_experiment.py --trial-data-dir ../trial_data_v2 --validate-selectors \
  --out ../experiment_report_v2.json 2>&1 | tee ../experiment_v2.log

# ===== Khối 3: ablation hysteresis (1–2 trial) =====
python3 10_hysteresis_ablation.py ../trial_data_v2/trial-01.json ../trial_data_v2/trial-02.json \
  --out ../hysteresis_ablation_v2.json 2>&1 | tee ../ablation_v2.log

# ===== Khối 4: Tier 2 qua Batch Orchestrator =====
# Nhánh tier2-run-tooling (PR #3) = main + ghi token + script 11. Tầng 1 giống hệt main.
cd ~/kltn-repo && git rev-parse HEAD   # ghi lại hash
# 4a. Source Online Boutique cho tool đọc source/PR
git clone https://github.com/GoogleCloudPlatform/microservices-demo ~/src/microservices-demo
# 4b. API Server (gatekeeper PR) — terminal riêng, cùng file DB
export GITHUB_TOKEN="..."              # tuỳ chọn; không có thì GitHub giới hạn 60 request/giờ
cd ~/kltn-repo/orchestration
python -m orchestration.cli_api --db ../kltn_tier2.sqlite3 --port 8080
# 4c. Batch 15 trial — terminal khác
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export TIER2_MODEL="claude-sonnet-5-5"  # mặc định trong code là "claude-sonnet-5" — đặt rõ model để ghi vào báo cáo
cd ~/kltn-repo/orchestration
for i in $(seq -w 1 15); do
  python -m orchestration.cli_batch --run-id trial-$i --namespace shared-eval \
    --owner-map ../infra/.cache/shared-eval.owner_map.json \
    --db ../kltn_tier2.sqlite3 --prometheus-url http://localhost:9090 \
    --source-root ~/src/microservices-demo --api-server http://localhost:8080 \
    --end-time "$END" \
    2>&1 | tee ../tier2_trial-$i.log
done
# Không truyền --deployed-at: clock tự đọc startTime harness pod (01/10) — trước END hơn 7 ngày.
# Lỗi ở trial nào: gửi nguyên log trial đó, KHÔNG sửa code.

# ===== Khối 5: xuất kết quả Tier 2 + recall gộp (chỉ đọc DB) =====
cd ~/kltn-repo/infra/scripts
python3 11_export_tier2_results.py --db ../../kltn_tier2.sqlite3 --ground-truth-dir ../ground_truth \
  --out-dir ../tier2_results 2>&1 | tee ../tier2_export.log
# Gửi lại: experiment_report_v2.json, hysteresis_ablation_v2.json, thư mục tier2_results/
# (tier2_report.json, tier2_edges.csv, tier2_edges.json, tier2_wrong.json), các file *.log.
