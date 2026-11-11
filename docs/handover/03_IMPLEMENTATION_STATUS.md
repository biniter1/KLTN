# 03 — Trạng thái triển khai

DONE chỉ ghi khi có bằng chứng (commit, test, hoặc lần chạy thật có file kết quả).

## 1. Repo và nhánh

| Nhánh | HEAD | Nội dung | Trạng thái |
|---|---|---|---|
| `main` | `50a99bd` | Merge PR #1 (`fee1bd9`, Tầng 1 code lại theo chốt 15/10 + VictoriaMetrics/Logs), revert `cac079f` | Code chuẩn |
| `tier1-port-gate` (PR #2) | — | Cổng port ở cửa ra (`NEEDS_MANUAL_PORT`, `PORT_CONFLICT`), `NOISE_ONLY_REVIEW`, `port_source = config` | **Chưa merge. Không merge khi Bin chưa nói.** Không có trong số liệu |
| `tier1-refactor-victoria` | — | Nhánh của PR #1 | Đã merge |
| `tier2-run-tooling` (PR #3) | `c62e37a` | Mọi việc Tầng 2 từ 02/11 + tài liệu bàn giao | Chưa merge |

[FACT] Remote báo "This repository moved" về `https://github.com/biniter1/KLTN.git` (cùng tên). Push vẫn thành công.

## 2. Test (chạy lại 11/11/2026 tại `c62e37a`, container sạch sau khi cài deps)

| Package | Kết quả | Lệnh |
|---|---|---|
| `tier1_inference` | 418 passed | `cd tier1_inference && python -m pytest -q` (cần `kubernetes`, nếu không 1 skip) |
| `tier2_agent` | 197 passed | `cd tier2_agent && python -m pytest -q` (pytest.ini dùng `--cov`, cần `pytest-cov`) |
| `infra` | 59 passed | `cd infra && python -m pytest -q` |
| `orchestration` | 123 passed | `cd orchestration && pip install -r requirements.txt -r requirements-dev.txt && python -m pytest` |

Trên `main` (30/10): tier1 414, orchestration 120, tier2 180, infra 46. Trên PR #2: tier1 424, orchestration 134.

## 3. Đã hoàn thành (có bằng chứng)

| Hạng mục | Bằng chứng |
|---|---|
| Hạ tầng shared-eval, thu 7 ngày traffic (lần 1 tới 08/10; lần 2 dùng cửa sổ 20–26/10) | `infra/trial_data/` (bản 08/10), `results/tier1/experiment_report_v2_20261102.json` |
| Tầng 1 code lại theo chốt 15/10, sửa L1–L11 | PR #1 `fee1bd9`, `docs/analysis/TIER1_CHANGE_PLAN.md` |
| Config Collector v2 (LLM + grounding + validate) | Chạy thật 02/11: 82 config record cho `shared-eval` |
| Tầng 2 code (Brain, 4 tool, Context Manager, Observer, hypothesis, lưu SQLite) | 197 test; chạy thật 15 trial v1/v2 |
| Orchestrator chạy 15/15 trial SUCCESS với Tầng 2 | `results/tier2_v1/export.log`, `results/tier2_v2/export.log` |
| Ghi token LLM (`llm_usage`) + script 11 xuất kết quả | `1e165fc`; `results/tier2_v*/tier2_report.json` |
| Client OpenAI cho Tầng 2 | `e43b72b` |
| `--end-time` (query cửa sổ quá khứ) | `9818c99`, dùng cho mọi số 02/11 |
| Benchmark bằng chứng code (script 12) | `cd6134f`, `results/tier2_benchmark/bench_v2.json` (89/90 phiên hợp lệ) |
| Tầng 2 v3 (F1–F6) | `14fce2b`, test pass |
| `no_match` trung tính | `33861dd`, test pass |
| `REJECTED_WITH_EVIDENCE` + metric hàng đợi | `556df77`, test pass |
| Ablation hysteresis | `results/tier1/hysteresis_ablation_v2_20261102.json` (chạy được, không phân biệt được 2 cơ chế) |

## 4. Có code nhưng chưa chạy thật / chưa kiểm

| Hạng mục | Thiếu gì |
|---|---|
| Tầng 2 v3 + no_match + REJECTED trên 15 trial | Chưa chạy (Azure hết hạn) |
| Benchmark v3 | 50/90 phiên rồi hết credit OpenAI (`results/tier2_benchmark/bench_v3_trace_partial.txt`) |
| Harness mới `e35c9d4` (gọi method gRPC nghiệp vụ thật qua `GRPC_BUSINESS_CALLS`, `ACTIVE_DATES` theo ngày lịch, ConfigMap `harness-protos`) | Chưa deploy |
| VictoriaMetrics/VictoriaLogs (`observed_log`) | Chưa chạy trên cụm; tên field log ztunnel lấy theo tài liệu |
| `KubernetesSelectorValidator` | Đã dùng thật ngày 02/11 (số 02/11 là validator thật). Số 30/10 và Chương 4 hiện tại vẫn validator giả |
| Metric hàng đợi trên dữ liệu 20–26/10 | Cần `trial_data_v2/` nằm trên VM Azure (đã mất?) — chỉ có smoke test trên dữ liệu 08/10 |

## 5. Chưa làm (chỉ có thiết kế hoặc chưa có)

- Baseline B3 (LLM one-shot) — đã cam kết trong đề cương v2.
- Config Collector v2.1 phần lớn: company_rules, regex_generic, Parse (a2), index wildcard ServiceEntry, `item_id` duy nhất, `targetPort` dạng tên, đọc probe trong collector. Đánh giá Collector vs regex v1.
- Orchestration v2: job runner, dashboard UI, snapshot/phát lại, dừng 2 cấp, `traffic_series`/`traffic_daily_counts`.
- Migration Planner: thuật toán xếp đợt (chưa thiết kế), sinh YAML, Policy Impact Validator, blast-radius override, Continuity Monitor.
- Fit trọng số chính thức (đã thử LOTO, quyết định giữ đặt tay).
- Chương 4.4.1, 4.4.4, 4.4.5, Chương 5, 6, Tóm tắt, Phụ lục; viết lại Chương 3 phần Tầng 2 theo v3.

## 6. Cách chạy

### Cài
```bash
pip install -r requirements.txt               # gốc repo
pip install -r tier1_inference/requirements.txt -r tier2_agent/requirements.txt
pip install -r orchestration/requirements.txt -r orchestration/requirements-dev.txt
```

### Dựng cụm và thu dữ liệu (đã chạy trên VM Azure Ubuntu)
`infra/scripts/00_check_prereqs.sh` → `01_install_tools.sh` → `02_create_cluster.sh` → `03_install_istio.sh` → `04_apply_telemetry_customization.sh` → `05_deploy_shared.sh` (hạ tầng dùng chung) → `05b_trim_waypoint_resources.sh` → `07_verify.sh`. Chờ 7 ngày. Chi tiết: `infra/README.md` (mục môi trường WSL2 trong đó đã cũ).

Harness mới cần render lại: `python infra/scenarios/render_manifests.py ... --sparse-window-start <YYYY-MM-DD> --window-days 7` (xem `--help`).

### Đánh giá Tầng 1
```bash
python infra/scripts/08_export_trial_data.py --end-time 2026-10-26T23:59:59Z ...   # xuất trial JSON
python infra/scripts/09_run_experiment.py --validate-selectors ...                 # Tầng 1 vs B1/B2 + hàng đợi
python infra/scripts/10_hysteresis_ablation.py ...                                  # cần series_daily_counts
```

### Tầng 1 + Tầng 2 qua orchestrator
Các bước đã dùng trên VM: `infra/scripts/vm_steps_tier1_tier2.sh`. Tóm tắt:
1. Prometheus lưu trữ từ bản sao `/data` trên `localhost:9091`, chờ `/-/ready` (WAL replay lâu).
2. Đặt key trong shell, **không** dán vào chat: `read -rs OPENAI_API_KEY && export OPENAI_API_KEY`; `export TIER2_PROVIDER=openai TIER2_MODEL=gpt-4.1-2025-04-14`.
3. API Server (cửa sổ tmux riêng, có `GITHUB_TOKEN`): `python -m orchestration.cli_api ...`.
4. Batch: `python -m orchestration.cli_batch --run-id trial-XX --end-time 2026-10-26T23:59:59Z ...` cho 15 trial.
5. Xuất: `python infra/scripts/11_export_tier2_results.py ...` (chỉ đọc SQLite).

### Benchmark Tầng 2 (không cần cụm)
```bash
python infra/scripts/12_tier2_code_benchmark.py --list-cases
python infra/scripts/12_tier2_code_benchmark.py --label v3 --repeats 3 ...   # cần OPENAI_API_KEY + checkout Online Boutique
```
Script từ chối chạy lại cùng `--label` (exit 2) và dừng khi hết credit.

**[ASSUMPTION]** Cờ đầy đủ của từng script ghi trong `--help` và `vm_steps_tier1_tier2.sh`; tài liệu này không chép lại để tránh sai lệch.

### Deploy
Không có deploy production. "Deploy" = chạy script infra trên VM.
