# Thiết kế chi tiết — Migration Planner: Policy Impact Validator & Zero Trust Continuity Monitor

Tài liệu này lấp phần còn treo của `Migration Planner` được liệt kê "chưa thiết kế chi tiết" ở `PROPOSAL.md` mục 9 — cụ thể là 2 thành phần phát sinh từ buổi thảo luận với cô Dung (23/09/2026), không phải thuật toán ưu tiên wave (`confidence × blast radius`, vẫn treo, xem mục 4).

Không lặp lại chi tiết đã có ở `TIER1_DESIGN.md`, `TIER2_DESIGN.md`, `ORCHESTRATION_DESIGN.md` — chỉ trỏ tới và tái dùng. Phần bảng dữ liệu/dashboard đầy đủ cho toàn bộ cơ chế con người can thiệp ở tài liệu này: xem `DASHBOARD_DESIGN.md`.

---

## 0. Vấn đề gốc & vị trí trong kiến trúc

Toàn bộ `TIER1_DESIGN.md`/`TIER2_DESIGN.md` trả lời câu hỏi **"dependency nào là thật?"**. Không tài liệu nào trả lời 3 câu hỏi vận hành sau, mà cô Dung chỉ ra là thiếu:

1. **Trước khi enforce 1 policy** (dù do Migration Planner tự sinh, hay do 1 network engineer tự viết tay) — policy đó áp vào có làm gãy dependency thật nào không? → **Policy Impact Validator** (mục 1).
2. **Trước cả khi có policy** — 1 kết luận `OBSERVED_STRONG` có nên tự động vào wave hay không, nếu đích đến quá nhạy cảm? → **Blast-radius override** (mục 1.5).
3. **Sau khi đã enforce** — Zero Trust có tiếp tục đúng theo thời gian không, hay có ai đó âm thầm mở 1 lỗ hổng (khẩn cấp, quên đóng, sửa tay ngoài quy trình), hoặc một quyền từng hợp lệ nay đã hết lý do tồn tại mà không ai rút lại? → **Zero Trust Continuity Monitor** (mục 2).

Ba thành phần này **không thay thế** Tier 1/Tier 2 — chúng tiêu thụ `dependency_current_state` (đã có, `ORCHESTRATION_DESIGN.md` mục 3) làm input, không suy luận dependency mới.

```
Tier 1 + Tier 2  ──────►  dependency_current_state  ──────►  Blast-radius override (mục 1.5)
(suy luận, đã chốt)              (đã có)                              │
                                     ▲                                 ▼
                                     │                    Policy Impact Validator (mục 1)
                                     │                        (GATE trước khi apply)
                                     │                                 │
                          Zero Trust Continuity Monitor  ◄──── NetworkPolicy được apply
                          (mục 2 — giám sát SAU khi apply,
                           liên tục + theo sự kiện, kể cả
                           quyền đã cấp nhưng hết lý do tồn tại)
```

---

## 1. Policy Impact Validator

### 1.1. Vai trò

Nhận 1 NetworkPolicy ứng viên — **không quan tâm nó từ đâu ra**: có thể là GitOps PR do chính Migration Planner tự sinh (tự kiểm tra trước khi mở PR), hoặc do 1 network engineer tự viết tay hoàn toàn độc lập (kịch bản cô Dung nêu). Trả về 1 Impact Report nói rõ: áp policy này có gãy dependency thật nào không.

### 1.2. Cơ chế — tái dùng gần như toàn bộ logic đã có

Không cần suy luận mới. 2 bước:

**Bước A — Resolve policy ứng viên thành tập cạnh nó THỰC SỰ cho phép.** Dùng đúng pipeline validate đã có ở `TIER1_DESIGN.md` mục 3b (query `Service.spec.selector`/pod trực tiếp từ K8s API tại thời điểm check, không cache), áp NGƯỢC: đọc `podSelector`/`namespaceSelector`/`ipBlock` của từng rule ingress/egress trong policy → resolve ra tập pod khớp → lấy `canonical_service` + `service_account` (như Tier 1 đã làm) → ghép với hướng (ingress/egress) và pod đích (chính policy đang áp) để ra tập `(source, dest, protocol, port)` mà policy này cho phép — gọi là `policy_allowed_edges`.

- `ipBlock` (external) → xử lý như `scope: EXTERNAL` (`TIER1_DESIGN.md` mục 8).
- Không có selector rõ ràng / selector giao nhau với Service khác → áp NGUYÊN VĂN luật cứng mục 3b: `NEEDS_MANUAL_SELECTOR`, không tự động validate tiếp, đẩy thẳng vào review.

**Bước B — Đối chiếu với `dependency_current_state`.** Với mỗi cạnh liên quan tới pod đích của policy (namespace + canonical_service trùng 1 trong 2 đầu):

```
cạnh THẬT (OBSERVED_STRONG / CONFIDENT / đã duyệt qua human_decisions)
  KHÔNG nằm trong policy_allowed_edges     → WOULD_BREAK   (nghiêm trọng)

cạnh trong policy_allowed_edges
  KHÔNG có bất kỳ bằng chứng nào (không traffic, không config_finding) → UNSUPPORTED_BY_EVIDENCE (cảnh báo)

cạnh THẬT nằm trong policy_allowed_edges   → COVERED       (ok)
```

**Verdict** (đã chốt với bạn):

```
BLOCKED               nếu có ≥1 WOULD_BREAK        — CHẶN CỨNG, không cho apply
PASSED_WITH_WARNINGS  nếu 0 WOULD_BREAK nhưng có ≥1 UNSUPPORTED_BY_EVIDENCE
PASSED                nếu không có gì bất thường
```

Lý do chặn cứng riêng cho `WOULD_BREAK` (không phải cả 2 loại): nhất quán với nguyên tắc bất đối xứng an toàn xuyên suốt đồ án (`PROPOSAL.md` mục 1) — bỏ sót 1 dependency thật (ở đây là "âm thầm cắt nó") nguy hiểm hơn hẳn giữ lại 1 rule chưa chắc chắn cần thiết. `UNSUPPORTED_BY_EVIDENCE` chỉ cảnh báo vì rất có thể là 1 dependency thật mà hệ thống chưa từng quan sát được (recall gap của chính hệ thống, không phải lỗi của policy).

### 1.3. Impact Report — schema

```jsonc
{
  "policy_name": "payment-netpol",
  "checked_at": "...",
  "checked_against_snapshot_id": "snap_0087",   // tính minh bạch, biết đang so với dữ liệu nào
  "verdict": "BLOCKED",
  "would_break": [
    {"identity_key": "...", "last_seen_count": 143, "last_seen_days_ago": 1}
  ],
  "unsupported_by_evidence": [
    {"proposed_edge": "...", "note": "Không tìm thấy traffic/config nào hỗ trợ rule này"}
  ],
  "needs_manual_selector": [ /* mục 3b TIER1_DESIGN.md */ ]
}
```

### 1.4. Nơi chạy

- **Route mới trên API Server**: `POST /policy-impact-check` (mở rộng `ORCHESTRATION_DESIGN.md` mục 6) — nhận 1 NetworkPolicy YAML, trả Impact Report. Dùng được cho cả 2 nguồn (Migration Planner tự gọi trước khi mở GitOps PR; CI pipeline gọi khi có PR NetworkPolicy do người viết tay).
- **CI gate**: 1 bước trong pipeline CI (đúng mô hình shift-left bạn đã làm ở DACN với ZAP/Cosign) — bất kỳ PR nào đổi file NetworkPolicy đều tự động gọi route trên; `verdict: BLOCKED` → CI fail, không cho merge.
- Migration Planner tự sinh policy về nguyên tắc sẽ luôn `PASSED` (vì nó sinh ra từ chính `dependency_current_state`) — chạy check này lên chính nó là phòng thủ lớp 2 (nhất quán "never trust, always verify" đã áp cho Tier 2 Agent ở `TIER2_DESIGN.md` mục 10, giờ áp luôn cho chính Migration Planner).

### 1.5. Blast-radius override — STRONG không phải lúc nào cũng miễn review

**Vấn đề**: `OBSERVED_STRONG` hiện tự động vào wave migrate (`ORCHESTRATION_DESIGN.md` mục 4), không qua review nào — hợp lý khi đích đến là service nghiệp vụ bình thường, nhưng nguy hiểm khi đích đến là tài nguyên nhạy cảm (namespace hệ thống, ra ngoài Internet, service có quyền đọc Secret). Độ tin cậy cao (score cao) không đồng nghĩa thiệt hại thấp nếu suy luận sai — đây là điểm khác của "confidence" và "blast radius", hai trục độc lập.

**Bổ sung luật, sửa `ORCHESTRATION_DESIGN.md` mục 4**:

```
OBSERVED_STRONG:
    đích KHÔNG nhạy cảm → vào wave migrate như cũ (không đổi)
    đích NHẠY CẢM       → PENDING_HUMAN_REVIEW (reason code: HIGH_BLAST_RADIUS)
                            — review TRƯỚC KHI Migration Planner tạo YAML,
                              sớm hơn 1 bước so với Policy Impact Validator
                              (Validator chỉ chạy khi ĐÃ có YAML ứng viên)
```

**Định nghĩa "nhạy cảm" — giá trị khởi tạo (placeholder, chưa đo thực nghiệm), giống tinh thần mọi ngưỡng khác trong đồ án (`θ_strong`, `error_ratio`...)**. Để giữ đúng phạm vi PoC, chỉ dùng 2 tiêu chí không cần thêm lời gọi K8s API mới (cả 2 đều đã có sẵn dữ liệu từ Tier 1):

```
destination_sensitivity = SENSITIVE nếu:
    destination_namespace ∈ {kube-system, cert-manager, <namespace cấu hình thủ công
                              cho danh sách nhạy cảm của cluster demo>}, HOẶC
    scope = EXTERNAL (ra ngoài Internet, TIER1_DESIGN.md mục 8)
```

**NOTE — điểm mở/diễn giải**: tiêu chí thứ 3 hợp lý về mặt lý thuyết — "destination có RBAC đọc Secret" — nhưng đòi hỏi Config Evidence Collector đọc thêm `RoleBinding`/`ClusterRoleBinding`, việc nó hiện KHÔNG làm (`CONFIG_EVIDENCE_COLLECTOR.md` mục 3 đã loại `Secret` khỏi phạm vi). Bỏ qua tiêu chí này ở PoC, chỉ dùng 2 tiêu chí trên — đơn giản hơn, đủ minh họa nguyên tắc "confidence cao không miễn review nếu rủi ro cao". Danh mục bảng dữ liệu/hàng đợi đầy đủ cho cơ chế này: xem `DASHBOARD_DESIGN.md` mục 2.

---

## 2. Zero Trust Continuity Monitor

### 2.1. Vấn đề cụ thể cần giải — ví dụ của cô Dung

Admin mở 1 port khẩn cấp để test → traffic đó tồn tại đủ K ngày, đủ đều → Tier 1 tự tính `OBSERVED_STRONG` → theo `ORCHESTRATION_DESIGN.md` mục 4, **thẳng vào wave migrate, KHÔNG qua review** → policy được enforce hợp lệ về mặt kỹ thuật nhưng admin quên gỡ, không ai biết đây là ngoại lệ tạm thời. Đây là lỗ hổng thật của thiết kế hiện tại, không phải tình huống hiếm.

Gốc rễ: hệ thống suy luận từ **hành vi quan sát được** (traffic), không phải **ý định** — traffic đủ lâu, đủ đều thì trông giống dependency thật bất kể lý do tồn tại. Không thể sửa bằng cách làm Tier 1 "thông minh hơn"; phải thêm 1 nguồn bằng chứng khác hẳn: **ai đã THAY ĐỔI gì, khi nào, qua kênh nào** — và 1 kênh chính thức để hành động khẩn cấp tự khai báo hạn dùng thay vì trông cậy trí nhớ con người.

### 2.2. Kênh chính thức cho ngoại lệ khẩn cấp — `emergency_exceptions`

Thay vì để hành động khẩn cấp lẫn vào traffic bình thường rồi hy vọng hệ thống "đủ giỏi" nhận ra, bắt hành động đó phải tự khai báo qua 1 kênh riêng, có hạn:

```jsonc
// emergency_exceptions — APPEND-ONLY, tương tự human_decisions
{
  "exception_id": "exc_042",
  "resource_kind": "NetworkPolicy" | "Service" | "...",
  "resource_namespace": "...",
  "resource_name": "...",
  "requested_by": "...",
  "reason": "test khẩn cấp sự cố #1234",
  "created_at": "...",
  "expires_at": "...",             // BẮT BUỘC — không có trường hợp "vô thời hạn"
  "status": "ACTIVE" | "EXPIRED" | "REVOKED" | "MADE_PERMANENT",
  "linked_config_change_id": null  // gán khi Drift Detector khớp được với 1 thay đổi thật (mục 2.4)
}
```

1 job định kỳ (cùng nhịp K-ngày với Tier 1, mục 2.6) quét `expires_at`: quá hạn mà vẫn `ACTIVE` → tự chuyển `EXPIRED` + đẩy thẳng vào `PENDING_HUMAN_REVIEW` với lý do "ngoại lệ khẩn cấp đã hết hạn — thu hồi, gia hạn, hay chính thức hoá (`MADE_PERMANENT` qua GitOps PR)?". Đây là điểm mấu chốt: **việc "quên" không còn phụ thuộc trí nhớ ai cả** — nó tự nổi lên.

Nhưng kênh này chỉ hiệu quả nếu admin THỰC SỰ dùng nó. Mục 2.3–2.4 là lưới an toàn cho trường hợp họ không dùng (đúng kịch bản cô Dung: sửa thẳng bằng `kubectl`, không khai báo gì).

### 2.3. Lưới an toàn — biết MỌI thay đổi thật, không chỉ thay đổi được khai báo

2 nguồn, bắt buộc dùng cả hai vì mỗi cái bù lỗ hổng của cái kia:

- **GitOps commit history** — mọi thay đổi qua Git có tác giả, review. **Không đủ**: hành động khẩn cấp thường bỏ qua đúng kênh này.
- **K8s API Audit Log** (`--audit-log-path`, hoặc audit webhook backend) — ghi MỌI request thay đổi state bất kể qua GitOps hay `kubectl edit`/`patch` trực tiếp. Đây là nguồn duy nhất bắt được hành động "tắt qua GitOps".

### 2.4. `config_change_log` — bảng mới, ghi MỌI thay đổi thật

```jsonc
{
  "change_id": "chg_017",
  "resource_kind": "NetworkPolicy",
  "resource_namespace": "...",
  "resource_name": "...",
  "change_type": "CREATE" | "UPDATE" | "DELETE",
  "source": "GITOPS" | "IMPERATIVE" | "UNKNOWN",
  "actor": "...",                    // service account (GitOps controller) hoặc user identity từ Audit Log
  "git_commit_sha": null,            // gán nếu source=GITOPS và tìm được commit tương ứng
  "detected_at": "...",
  "orphan": false,                   // true nếu Drift Detector không truy được về commit nào (mục 2.5)
  "reviewed": false
}
```

Ghi qua 2 đường: (1) webhook nhận sự kiện Audit Log gần real-time (mục 2.6) — ghi ngay khi có mutation; (2) đối chiếu lại với Git commit history để điền `git_commit_sha`/`source=GITOPS` khi tìm thấy khớp (theo `resourceVersion`/thời điểm gần commit).

### 2.5. Drift Detector — logic

Chạy theo 2 nhịp (mục 2.6):

1. Liệt kê mọi NetworkPolicy/Service đang LIVE liên quan tới cạnh đã enforce.
2. Với mỗi resource, tìm dòng `config_change_log` gần nhất: có `git_commit_sha` hợp lệ → **đã quy trách nhiệm được (attributed)**, không orphan.
3. Không tìm được (chỉ có `IMPERATIVE`, hoặc hoàn toàn không có log nào — kể cả trường hợp Audit Log bị gián đoạn) → `orphan = true`.
4. Đối chiếu thêm với bằng chứng traffic/config của chính rule đó (tái dùng dữ liệu Tier 1 đã có):
   - Orphan + KHÔNG có traffic/config nào hỗ trợ → mức nghiêm trọng cao nhất (rất có thể là lỗ hổng bị bỏ quên thật).
   - Orphan nhưng CÓ traffic thật hỗ trợ → mức thấp hơn (có vẻ là nhu cầu thật, chỉ là chưa ai hợp thức hoá qua GitOps).
5. Mọi `orphan=true` → đẩy vào `PENDING_HUMAN_REVIEW` (tái dùng đúng exception queue đã có, `ORCHESTRATION_DESIGN.md` mục 4) với reason code `UNATTRIBUTED_CONFIG_CHANGE`, không tự động thu hồi (đúng nguyên tắc "không có tiến trình nào tự enforce mà không qua người" đã có).

### 2.5b. Ví dụ minh họa đầy đủ — "mở port khẩn cấp rồi quên đóng"

Ráp lại toàn bộ cơ chế trên bằng đúng kịch bản cô Dung nêu, từng bước:

1. **Admin mở port** bằng `kubectl` (không qua GitOps). Audit Log Webhook bắt gần thời gian thực → ghi `config_change_log` (`source=IMPERATIVE`, `git_commit_sha=null`).
   - Nếu admin KHÔNG khai báo `emergency_exceptions` → ngay lập tức `orphan=true` → vào review với `UNATTRIBUTED_CONFIG_CHANGE` (phát hiện gần như tức thì).
   - Nếu CÓ khai báo (đúng quy trình) → tạm không orphan, chờ `expires_at`.
2. **Thời gian trôi qua, admin quên gia hạn/đóng.** Không có sự kiện mới nào xảy ra (port vẫn mở y nguyên, im lìm) — nên cơ chế event-based (bước 1) không bắt được gì thêm. **Sweep định kỳ (mục 2.6) là cơ chế DUY NHẤT bắt được trường hợp này**: quét `emergency_exceptions WHERE expires_at < now() AND status='ACTIVE'` → tự chuyển `EXPIRED` → vào review.
3. **Drift Detector không chỉ báo "hết hạn" suông** — nó đối chiếu ngược `dependency_current_state` (bước 4 mục 2.5, tái dùng thẳng view của Tier 1, không viết logic đo traffic mới):
   - Còn traffic thật (`OBSERVED_STRONG` gần đây) → có thể đang được dùng thật, mức độ THẤP — vẫn cần người xác nhận nhưng ưu tiên thấp hơn.
   - Không còn traffic (`NOT_OBSERVED` từ lúc mở tới giờ) → đúng kịch bản "mở toang không ai dùng" — mức độ CAO, ưu tiên đầu hàng đợi.
4. **Hiện trên dashboard đầy đủ ngữ cảnh** (port nào, ai mở, từ khi nào, có traffic hỗ trợ không, hết hạn bao lâu) → admin nhìn vào nhận ra ngay "quên thật" → đóng lại port, hoặc nếu vẫn cần thì chuyển `emergency_exceptions.status → MADE_PERMANENT` (hợp thức hoá qua GitOps PR, không còn là "khẩn cấp" nữa).

### 2.6. Continuous hay event-based — cả hai, trả lời 2 câu khác nhau

- **Event-triggered** (qua Audit Log webhook, gần real-time): ghi `config_change_log` ngay khi có mutation; có thể chạy luôn 1 lượt Drift Detector chỉ trên đúng resource vừa đổi — phát hiện nhanh.
- **Sweep định kỳ** (bắt buộc, không thể thay bằng event-trigger): dùng đúng nhịp K-ngày mà Tier 1 đã có cho snapshot/hysteresis (`TIER1_DESIGN.md` mục 5b) — không bày thêm lịch trình mới. Lý do bắt buộc: vấn đề "quên đóng lại" về bản chất là vấn đề THỜI GIAN TRÔI QUA mà không ai quay lại kiểm tra — event-trigger chỉ bắt được LÚC xảy ra thay đổi, không bắt được việc "đã lâu rồi mà chưa ai xử lý" (minh họa cụ thể ở mục 2.5b bước 2). Đây cũng là cách áp dụng nguyên tắc *continuous diagnostics and mitigation* của NIST SP 800-207 (tài liệu compliance bạn đã dùng ở DACN) — Zero Trust không phải sự kiện di trú 1 lần.

### 2.7. Dashboard

Xem `DASHBOARD_DESIGN.md` mục 2–3 cho danh mục đầy đủ reason code, bảng dữ liệu, và schema `human_decisions` tổng quát. Tóm tắt: `config_change_log`/`emergency_exceptions` xuất hiện ở Lớp A (đọc, `ORCHESTRATION_DESIGN.md` mục 5); `UNATTRIBUTED_CONFIG_CHANGE`/hết hạn/`STALE_PERMISSION` (mục 2.8) đẩy vào Lớp B (review, có ghi).

### 2.8. Suy giảm quyền theo thời gian (`STALE_PERMISSION`) — hướng ngược lại với orphan

Mục 2.2–2.7 xử lý trường hợp có ai đó ÂM THẦM THAY ĐỔI config. Còn một hướng khác, không liên quan tới thay đổi config nào cả: 1 dependency từng thật (đã `OBSERVED_STRONG`, đã enforce), sau đó traffic biến mất (ví dụ: service dùng để test bị gỡ sau khi hết đợt kiểm thử 7 ngày) — NetworkPolicy rule vẫn còn nguyên, không ai chủ động rút lại quyền đã cấp, vi phạm nguyên tắc least-privilege dù không có "sự cố" hay "thay đổi trái phép" nào xảy ra cả.

Tái dùng đúng cơ chế hysteresis đã có (`TIER1_DESIGN.md` mục 5b), không cần logic đo mới — chỉ thêm MỘT bước đọc ngược, chạy cùng nhịp sweep (mục 2.6):

```
Mỗi chu kỳ sweep:
  Với mọi cạnh ĐANG có NetworkPolicy rule enforce thật:
    Nếu Tier 1 vừa hạ nó khỏi STRONG (score < θ_demote, mục 5b)
    → reason code STALE_PERMISSION → PENDING_HUMAN_REVIEW
    → KHÔNG tự động thu hồi rule (nguyên tắc bất đối xứng an toàn — lỡ đo
      sai mà tự rút quyền có thể làm gãy production; để người quyết định)
```

**Điểm hay của cách vá này**: không cần bảng mới, không cần thuật toán đo mới — chỉ cần đọc lại chính output đã có sẵn của Tier 1 (`previous_status` đã lưu ở mục 5b) mỗi chu kỳ sweep, và định tuyến đúng chỗ (review-queue) khi phát hiện demote trên 1 cạnh ĐANG enforce thật.

---

## 3. Framing bảo mật cho luận văn

Toàn bộ mục 1.5 và mục 2 là áp dụng ĐÚNG nguyên tắc "never trust, always verify" mà `TIER2_DESIGN.md` mục 10 đã dùng cho AI Agent — nhưng lần này cho **chính con người vận hành**, không chỉ cho Agent:

- Deny-by-default: `emergency_exceptions` không có "vô thời hạn" — mọi ngoại lệ đều phải hết hạn trừ khi được chính thức hoá lại.
- Never-trust-always-verify: mọi thay đổi network config — dù qua GitOps hay tay — đều bị Continuity Monitor kiểm tra lại, không có kênh nào "được tin tưởng mặc định". Kể cả tin cậy cao (`OBSERVED_STRONG`) cũng không tự động miễn review nếu rủi ro (blast radius) đủ cao — độ tin cậy và rủi ro là 2 trục độc lập, mục 1.5.
- Least privilege không phải trạng thái tĩnh: quyền từng hợp lệ có thể hết lý do tồn tại theo thời gian (mục 2.8) — phải chủ động rà soát lại, không mặc định "đã cấp rồi thì cứ giữ".
- Đây chính là điểm trả lời trực tiếp câu hỏi mở ở `README.md` (gói Tier 2 đã giao) mục "Điểm mở/diễn giải" #3 — về việc `OBSERVED_STRONG` hiện không qua review nào trước khi enforce.

Trích dẫn được: NIST SP 800-207 (continuous monitoring/CDM), đã có sẵn trong bộ tài liệu compliance từ DACN.

---

## 4. Chưa thiết kế / ngoài phạm vi

- Thuật toán ưu tiên wave (`confidence × blast radius`) — vẫn treo, không thuộc phạm vi tài liệu này.
- Alerting/paging thật ra ngoài hệ thống (email/Slack) — chỉ dừng ở ghi vào hàng đợi review, không có notification system ngoài.
- Audit Log cho môi trường multi-cluster/multi-tenant — PoC giả định 1 cluster `kind`/minikube duy nhất.
- Độ chính xác của việc khớp `config_change_log` với `git_commit_sha` khi có độ trễ lớn giữa lúc Git merge và lúc controller GitOps thực sự apply — cần đo thực nghiệm, đặt ngưỡng thời gian khớp hợp lý (chưa có giá trị placeholder).
- Tiêu chí RBAC-đọc-Secret cho `destination_sensitivity` (mục 1.5) — cần Config Evidence Collector đọc thêm RoleBinding, hiện không làm.
- **Cổng chặn TRƯỚC khi thay đổi (preventive admission gate)** — bắt buộc khai báo `emergency_exceptions` TRƯỚC khi được phép đổi config (qua K8s `ValidatingAdmissionWebhook`), thay vì chỉ phát hiện SAU (mô hình DETECTIVE hiện tại, mục 2). Cân nhắc và không làm ở PoC này, vì 3 lý do: (1) thêm hẳn 1 K8s control-plane component mới, vượt phạm vi đã giảm liên tục xuyên suốt đồ án (`ORCHESTRATION_DESIGN.md` mục 8: "không watch/controller"); (2) rủi ro vận hành thật — nếu webhook fail-closed mà nó chết đúng lúc cần hành động khẩn cấp thật, hệ thống tự làm gãy chính mục đích của break-glass (fail-open thì lại vô nghĩa, quay về DETECTIVE); (3) đây chính là việc Kyverno/OPA Gatekeeper làm, đã dùng ở đồ án DACN riêng — đưa cùng công nghệ vào KLTN sẽ làm mờ ranh giới 2 đồ án. Ghi nhận đây là hướng mở rộng hợp lý cho một hệ thống production thật, không phải thiếu sót của PoC.
