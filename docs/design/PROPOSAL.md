# Tổng quan đồ án — Khung suy luận sự phụ thuộc hai tầng cho Zero Trust NetworkPolicy trên Kubernetes

Tài liệu này là điểm vào (entry point) cho toàn bộ đồ án — mô tả bức tranh tổng, cách các tài liệu thiết kế chi tiết khác (`TIER1_DESIGN.md`, `TRAFFIC_COLLECTION_PIPELINE.md`, `CONFIG_EVIDENCE_COLLECTOR.md`, `ORCHESTRATION_DESIGN.md`) khớp vào nhau, và hiện trạng phần chưa xong. Không lặp lại chi tiết đã có trong các tài liệu đó — chỉ tóm tắt và trỏ tới đúng chỗ.

**Quan trọng**: tài liệu này dùng khung **hai tầng** (Tier 1 gộp cả runtime + config evidence, Tier 2 = AI Agent) — đúng với kiến trúc kỹ thuật thật đang được xây. Bản đề cương chính thức nộp cho khoa (`đề cương chi tiết KLTN`) cố tình dùng khung **ba tầng** cũ để khớp với những gì đã trao đổi qua email với cô Dung — hai tài liệu khác mục đích, không mâu thuẫn nhau, xem thêm mục 8.

---

## 1. Vấn đề & động lực

Trong các hệ thống Kubernetes vận hành lâu năm, việc áp dụng NetworkPolicy default-deny (nguyên tắc lõi của Zero Trust) thường bị trì hoãn vì dependency giữa các service chưa được tài liệu hóa đầy đủ. Enforce sai một policy có thể làm gián đoạn production — đây là rủi ro bất đối xứng: bỏ sót một dependency thật (false negative) nguy hiểm hơn nhiều so với giữ lại một quyền truy cập chưa chắc chắn (false positive). Nguyên tắc bất đối xứng an toàn này xuyên suốt mọi quyết định thiết kế trong đồ án.

**Khoảng trống so với công cụ hiện có** (Kubescape/ARMO, Otterize): các công cụ này sinh policy theo mô hình quan sát traffic một lần (single-shot) trong một cửa sổ cố định — không có cơ chế đánh giá độ tin cậy, không chủ động thu thập thêm bằng chứng khi traffic thưa hoặc mơ hồ. Nhóm dependency "khó quan sát" (CronJob dài ngày, chỉ ghi trong code comment/tài liệu nội bộ) chính là nhóm dễ bị bỏ sót nhất và cũng nguy hiểm nhất khi enforce sai.

**Hướng nghiên cứu liên quan** (KubeIntellect, KubeGuard — AI agent hỗ trợ vận hành Kubernetes): tập trung vận hành/hardening tổng quát, chưa giải quyết trực tiếp bài toán suy luận dependency có kiểm soát độ tin cậy phục vụ migration NetworkPolicy an toàn.

---

## 2. Kiến trúc tổng quan

```
                Cạnh dependency ứng viên
                          │
      ┌───────────────────┴───────────────────┐
      ▼                                         ▼
Traffic thực tế                        Bằng chứng cấu hình
(Istio ambient/Prometheus)             (Config Evidence Collector:
      │                                 thu thập K8s config → LLM trích
      │                                 xuất → xác thực tất định K8s/Istio API)
      └───────────────────┬───────────────────┘
                          ▼
                      Tier 1
        (tổng hợp 2 nguồn, tự kết luận nếu đủ tin cậy)
                          │
              ┌───────────┼────────────┐
              ▼           ▼            ▼
        OBSERVED_STRONG  escalate   NOT_OBSERVED
        → wave migrate*  │          + config mạnh
                          ▼          → hàng đợi người
                      Tier 2
              (AI Agent, chỉ chạy khi Tier 1
               chưa đủ căn cứ)
                          │
                          ▼
                  Migration Planner
        (wave plan · exception queue · GitOps PR ·
         Policy Impact Validator · Zero Trust
         Continuity Monitor — MIGRATION_PLANNER_DESIGN.md)
                          │
                          ▼
              Con người phê duyệt → NetworkPolicy thật
```
*trừ khi đích nhạy cảm (blast-radius override) → vẫn cần người trước, xem `MIGRATION_PLANNER_DESIGN.md` mục 1.5.

Hai nguyên tắc kiến trúc xuyên suốt:

1. **Chỉ 2 tầng suy luận thật** — Tier 1 và Tier 2 là hai điểm có quyền "tự kết luận". Config Evidence Collector **không phải một tầng** — nó không có quyền kết luận gì, chỉ cấp bằng chứng đầu vào cho Tier 1 (xem `CONFIG_EVIDENCE_COLLECTOR.md` mục 0 để hiểu tại sao đây là phát hiện kiến trúc quan trọng, không phải chi tiết đặt tên). Bước LLM bên trong Collector cũng **không phải Tier 2**: đó là 1 lần gọi trích xuất có schema, không tool, không vòng lặp, không kết luận — luồng tier vẫn một chiều Tier 1 → Tier 2.
2. **`count > 0` (có traffic thật) là điều kiện cần cứng cho tự động enforce** — dù bằng chứng cấu hình hay AI Agent thuyết phục tới đâu, không gì được phép một mình kích hoạt auto-enforce khi chưa từng quan sát traffic thật. Đây là luật an toàn quan trọng nhất của toàn hệ thống.

---

## 3. Tier 1 — Inference Layer (đã chốt, xem `TIER1_DESIGN.md`)

Tự kết luận khi đủ tin cậy, escalate phần còn lại. Tiêu thụ song song 2 nguồn:

- **Candidate discovery**: `C = C_observed ∪ C_potential` — hợp nhất cặp có traffic thật với cặp suy ra từ config, để không bỏ sót dependency thưa.
- **Định danh ổn định**: `identity_key = (namespace, canonical_service, service_account, ...)` — dùng nhãn Istio, không dùng tên workload thô.
- **Scoring**: `sigmoid(w1·recency + w2·log_volume + ...)`, trọng số học bằng logistic regression, không đoán tay.
- **5 trạng thái đầu ra**: `OBSERVED_STRONG` (tự kết luận, dừng), `OBSERVED_WEAK`/`UNKNOWN` (escalate Tier 2), `NOT_OBSERVED` (rẽ nhánh riêng — xem mục 4), `NEEDS_MANUAL_SELECTOR` (validation sinh YAML thất bại).
- **Hysteresis bất đối xứng** (`θ_promote=0.68` / `θ_demote=0.58`) chống nhấp nháy trạng thái giữa các lần quan sát.
- **Baseline đánh giá component-level** (`TIER1_DESIGN.md` mục 13): 2 baseline nội bộ (quy tắc `count>0` naive, ngưỡng cứng cũ) chạy trên cùng R=15 lần + ablation riêng cho hysteresis — cô lập đóng góp thật của scoring/hysteresis, khác với 4 baseline A–D ở mục 7 (so sánh toàn hệ thống với công cụ/phương pháp bên ngoài).

Bằng chứng runtime thu thập qua Istio ambient (ztunnel + waypoint, xem `infra/README.md` để biết lý do đổi từ sidecar sang ambient, đổi ngày 2026-09-26)/Prometheus (chi tiết cơ chế vật lý: `TRAFFIC_COLLECTION_PIPELINE.md`). Bằng chứng cấu hình thu thập qua Config Evidence Collector (chi tiết pipeline: `CONFIG_EVIDENCE_COLLECTOR.md`).

---

## 4. Config Evidence Collector (chốt thiết kế v2 ngày 29/09, xem `CONFIG_EVIDENCE_COLLECTOR.md`)

Không phải một tầng — một module cấp bằng chứng cấu hình cho Tier 1. **v2 thay bộ parser rule-based của v1** vì config thực tế viết địa chỉ dưới vô số dạng (connection string, broker list, cờ `args`, file YAML mount từ ConfigMap, host/port tách biến với tên tùy ý) — phủ bằng rule không bao giờ đủ, mỗi rule thiếu là một false negative im lặng.

Pipeline v2 — LLM kẹp giữa hai lớp tất định:

1. **Thu thập** theo pod template của workload (bắt được cả CronJob chưa chạy / `replicas=0`): env, envFrom, valueFrom, command/args, initContainers, ConfigMap (biến + file mount), image ENV/ENTRYPOINT/CMD, annotations. **Không đọc Secret.**
2. **Hợp nhất effective config** theo đúng thứ tự ưu tiên của K8s, expand `$(VAR)`, giữ provenance — tránh LLM trích cả giá trị đã bị ghi đè.
3. **Phễu lọc bảo thủ + chuẩn hóa**: chỉ bỏ cái chắc chắn vô nghĩa (rỗng, boolean, binary), lọc theo hình dạng giá trị không theo tên biến, log mọi thứ bị bỏ; redact credential trước khi gửi ra ngoài.
4. **LLM extraction** (1 lần gọi/chunk, temperature 0, structured output, cache theo hash để tái lập): chỉ trích địa chỉ + **đoạn nguyên văn**; LLM **không được biết** danh sách Service.
5. **Validation tất định, rẽ nhánh**: grounding (không có nguyên văn → loại), phân giải host qua K8s/Istio API (Service có/không selector, ExternalName, EndpointSlice, ServiceEntry, VirtualService, domain ngoài), port → `targetPort`, chuẩn hóa `identity_key`, bỏ tự tham chiếu.

Giao diện ra Tier 1 **không đổi**: nhãn `config_finding` (đã-xác-nhận hay chưa), không tự tính confidence. Code v1 giữ lại làm **baseline regex** để so sánh có số liệu (mục 7).

**Luật tiêu thụ 2 trục** (Trục A Safety / Trục B Cost) quyết định `NOT_OBSERVED` đi đâu:
- `FOUND_STRUCTURAL`/`FOUND_EMBEDDED` sạch → hàng đợi review con người, bỏ qua Tier 2 (tiết kiệm chi phí AI).
- `FOUND_UNRESOLVED`/`NOT_FOUND` → escalate Tier 2 (mù cả traffic lẫn config).

---

## 5. Tier 2 — AI Agent (**đang thiết kế, chưa hoàn chỉnh**)

Khác các phần trên, mục này ghi lại **hiện trạng**, không phải bản chốt cuối. Đã thống nhất được:

- **Khi nào được gọi**: `OBSERVED_WEAK`/`UNKNOWN`, hoặc `NOT_OBSERVED` với config mù.
- **Kiến trúc vòng lặp**: ReAct thuần (reason → act → observe → lặp) — không dùng Plan-Execute riêng, không dùng multi-agent, vì tác vụ đủ hẹp (xác minh 1 cạnh, ≤4 tool đã biết) nên ReAct tự "lập kế hoạch lại" miễn phí mỗi vòng.
- **4 tool**: đọc code comment (local), đọc PR description (qua GitHub API, có gatekeeper giữ token — Agent không bao giờ cầm token trực tiếp), đọc tài liệu nội bộ, mở rộng cửa sổ traffic.
- **I/O có cấu trúc**: LLM bắt buộc trả đúng 1 trong 2 dạng JSON (`call_tool` hoặc `conclude`) mỗi vòng, ép bằng structured output của API, không hy vọng LLM tự giác.
- **4 điều kiện dừng**: đạt ngưỡng + có bằng chứng chéo từ Tier 1 → `CONFIDENT`; cạn tool → `NEEDS_MONITORING`/`INSUFFICIENT_BASIS`; vượt số vòng lặp tối đa → xử lý như cạn tool; phát hiện bằng chứng mâu thuẫn trực tiếp → `CONFLICTING_EVIDENCE` (luôn vào hàng đợi người, không qua điểm số).
- **Không train/fine-tune model** — không có tập dữ liệu khách quan ở quy mô này; "trả lời theo ý muốn" đạt được bằng structured output + prompt engineering trên LLM pretrained (cùng cách Config Collector v2 dùng LLM: pretrained, không train).
- **Bảo mật**: Agent chỉ có quyền đọc, mọi lời gọi tool qua gatekeeper (API Server) có RBAC hẹp + audit log — phòng rủi ro evidence injection (code comment cố tình viết sai để đánh lừa AI).

**Chưa chốt**: câu hỏi đang dở — chính xác Tier 2 nhận được gì làm input (đã thống nhất là nguyên schema evidence của Tier 1, nhưng chưa xác nhận có cần thêm gì khác không). Cơ chế Bayesian log-odds/likelihood ratio, self-consistency (k=5 lần) và corroboration rule kế thừa từ một phiên trước — **chưa được xác nhận lại**, đang chờ xem xét kỹ trước khi coi là chính thức.

---

## 6. Coordination Layer (đã chốt, xem `ORCHESTRATION_DESIGN.md`)

Hai loại tiến trình khác vòng đời:
- **Batch Orchestrator** (ngắn hạn, chạy per-trial): cấy kịch bản → thu traffic → chạy Config Collector → gộp SQLite → chạy Tier 1 → escalate Tier 2 nếu cần → thoát.
- **API Server** (dài hạn, luôn sống): phục vụ dashboard (đọc), màn hình review con người (ghi có audit), và gatekeeper cho Tier 2.

Lưu trữ tách bảng theo quyền ghi (`dependency_evidence` chỉ Orchestrator ghi, `human_decisions` chỉ API Server ghi, append-only) để quyết định con người không bao giờ bị ghi đè khi pipeline chạy lại. Thiết kế đầy đủ của chính dashboard (mọi điểm cần con người can thiệp, danh mục bảng dữ liệu, schema quyết định tổng quát): `DASHBOARD_DESIGN.md`.

---

## 7. Đánh giá thực nghiệm

- **R=15 lần chạy độc lập**, mỗi lần cấy một kịch bản dependency ẩn ngẫu nhiên khác nhau (không phải chạy lặp trên cùng dữ liệu) — đo `mean ± CI 95%` của precision/recall, tránh kết luận dựa trên một lần đo may rủi.
- **4 baseline so sánh**: (A) traffic-sniffing một lần (mô phỏng Kubescape/Otterize), (B) rule-based với decision tree cố định, (C) LLM one-shot không vòng lặp, (D) LLM phân tích toàn hệ thống một lần rồi sinh rule tĩnh.
- **Đánh giá riêng Config Collector** (`CONFIG_EVIDENCE_COLLECTOR.md` mục 9): đo từng tầng (collection coverage, funnel loss, extraction recall/precision, tỷ lệ hallucination bị grounding chặn, recall/precision cuối của `C_potential`), so với **baseline regex** (code v1) trên cùng R=15 bằng kiểm định bắt cặp Wilcoxon. Số liệu chính chỉ tính trên dependency **tự cấy** (tránh rủi ro LLM "thuộc" Online Boutique).
- **Môi trường**: cluster local (`kind`/minikube), Online Boutique làm nền, tự cấy thêm service/dependency giả theo từng kịch bản cần chứng minh (không dùng dữ liệu tĩnh tự bịa, không cần ngân sách cloud).
- **Ground truth**: tự thiết kế vì tự cấy kịch bản — phía config phải phủ đủ các dạng reference (URL, host/port tách biến, connection string, broker list, cờ args, file mount, initContainer, CronJob chưa chạy, giá trị bị ghi đè, secretKeyRef, mồi nhử trùng tên, chuỗi injection…) đã liệt kê ở `CONFIG_EVIDENCE_COLLECTOR.md` mục 9.3, tránh chọn case tùy tiện.

---

## 8. Quan hệ với đề cương chính thức

Bản đề cương nộp khoa (`DeCuongKLTN_23520149_HaSonBin.pdf`) mô tả cùng một hệ thống này nhưng dùng khung 3 tầng cũ (Tầng 1 traffic → Tầng 2 config → Tầng 3 AI Agent) vì đó là khung đã trao đổi với cô Dung qua email trước khi có phát hiện kiến trúc 2 tầng. Tài liệu này (bản kỹ thuật nội bộ) và đề cương (bản nộp chính thức) **cố tình không đồng bộ thuật ngữ** — không phải sai sót. Nếu quyết định trình bày lại 2 tầng với cô Dung sau này, đề cương cần cập nhật lại cho khớp.

Việc Config Collector v2 dùng LLM để trích xuất (29/09) cũng **chưa trao đổi với cô Dung** — nên gom chung với việc đổi 3 tầng → 2 tầng khi trình bày.

---

## 9. Trạng thái hiện tại

| Phần | Trạng thái | Tài liệu |
|---|---|---|
| Tier 1 (Inference Layer) | Chốt, đã stress-test | `TIER1_DESIGN.md` |
| Traffic Collection Pipeline | Chốt (mesh mode: Istio ambient, đổi từ sidecar ngày 2026-09-26) | `TRAFFIC_COLLECTION_PIPELINE.md` |
| Config Evidence Collector | **Chốt thiết kế v2 (29/09 — LLM-assisted extraction), chưa implement code v2**; code v1 chuyển thành baseline regex; chưa báo cô Dung | `CONFIG_EVIDENCE_COLLECTOR.md` |
| Coordination Layer | Chốt | `ORCHESTRATION_DESIGN.md` |
| Tier 2 (AI Agent) | **Đang thiết kế** — input schema chưa chốt, Bayesian/self-consistency chưa xác nhận lại | `TIER2_DESIGN.md` |
| Migration Planner — Policy Impact Validator + Zero Trust Continuity Monitor | Chốt (đảm bảo Zero Trust được giữ vững SAU migration/enforce, kể cả với policy hand-written; gồm cả blast-radius override và suy giảm quyền theo thời gian) | `MIGRATION_PLANNER_DESIGN.md` |
| Migration Planner — thuật toán ưu tiên wave (wave prioritization algorithm) | Chưa thiết kế chi tiết | — |
| Dashboard & cơ chế con người can thiệp | Chốt (sơ đồ end-to-end, danh mục `reason_code`, schema quyết định tổng quát, danh mục bảng dữ liệu) | `DASHBOARD_DESIGN.md` |
| Đề cương chính thức (3 tầng) | Đã nộp khoa | `DeCuongKLTN_23520149_HaSonBin.pdf` |
| Hạ tầng thu dữ liệu thật (`infra/`) | Đã dựng script + test, mesh đổi sang ambient; kiến trúc "dùng chung hạ tầng" (giải capacity) đã thống nhất với cô Dung nhưng **chưa implement code** | `infra/README.md` |
