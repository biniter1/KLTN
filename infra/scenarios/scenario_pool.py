"""
Bộ sinh kịch bản cấy cho R=15 trial độc lập — PROPOSAL.md mục 7 ("mỗi lần cấy một
kịch bản dependency ẩn ngẫu nhiên khác nhau"), CONFIG_EVIDENCE_COLLECTOR.md mục 2
("tự inject thêm service/dependency giả theo đúng kịch bản test cần chứng minh").

Không tự chạy được gì trên cluster — module THUẦN PYTHON, không phụ thuộc `kubernetes`/
`requests`, để có thể unit-test đầy đủ mà không cần cluster thật (đúng tinh thần
`tier1/config_collector.py`/`traffic_collector.py`: tách thuật toán khỏi I/O thật).
Đầu ra của `generate_trial()` là input cho 2 việc:
  1. `scripts/05_deploy_trial.sh` (đọc JSON xuất ra để dựng harness Deployment thật).
  2. `tier1.experiment.TrialInput.ground_truth` (sau khi thu thập traffic/config thật
     bằng `traffic_collector.py`/`config_collector.py`, xem `08_export_trial_data.py`).

Đồ thị "thật" của Online Boutique (baseline, mục ONLINE_BOUTIQUE_REAL_EDGES) lấy từ
kiến trúc công khai của GoogleCloudPlatform/microservices-demo — đây là các cạnh LUÔN
tồn tại kể cả không cấy gì thêm, dùng làm nhiễu nền thực tế (Tier 1 vẫn phải phân biệt
đúng những cạnh này với cạnh mới bị cấy, không phải nhiệm vụ của trial nhưng ảnh hưởng
candidate set thật).

6 LOẠI kịch bản cấy — bám đúng các lát cắt TIER1_DESIGN.md mục 13.3 + trạng thái mục 5:
  STRONG_HIDDEN         : traffic mạnh, đều đặn -> kỳ vọng OBSERVED_STRONG (ground truth=True)
  SPARSE_HIDDEN         : traffic thưa (distinct_days<=2) -> ground truth=True, nơi B2 dự kiến
                          MISS nặng nhất (mục 13.3)
  CONFIG_ONLY_CLEAN     : chỉ khai config (env trỏ đúng 1 Service thật), KHÔNG sinh traffic
                          -> NOT_OBSERVED + needs_human_review, ground truth=True (dependency
                          có thật nhưng chưa/không còn hoạt động)
  CONFIG_ONLY_UNRESOLVED: khai config trỏ 1 tên KHÔNG khớp Service nào (hoặc đã bị xóa) ->
                          NOT_OBSERVED + escalate Tier 2, ground truth=False (không phải
                          dependency thật, chỉ là biến môi trường không liên quan)
  NOISE_HEALTHCHECK     : traffic mạnh nhưng toàn path health-check -> ground truth=False,
                          nơi B1 dự kiến precision thấp (mục 13.3)
  NOISE_MONITORING      : traffic mạnh từ 1 nguồn nằm trong monitoring_source_services
                          (mục 2 TIER1_DESIGN.md) -> ground truth=False, cùng vai trò NOISE_HEALTHCHECK
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

PairKey = tuple[str, str, str, str]  # (source_ns, source_svc, dest_ns, dest_svc)

NAMESPACE = "default"  # 1 trial = 1 Online Boutique riêng, mỗi trial nằm trong 1 K8s namespace
                        # RIÊNG (đặt tên namespace = trial_id) — bên trong namespace đó mọi
                        # Service vẫn ở "default"-style flat, không cần namespace lồng thêm.

# ---------------------------------------------------------------------------
# Đồ thị "thật" của Online Boutique — luôn tồn tại, KHÔNG phải kịch bản cấy.
# ---------------------------------------------------------------------------

ONLINE_BOUTIQUE_SERVICES = [
    "frontend", "cartservice", "productcatalogservice", "currencyservice",
    "paymentservice", "shippingservice", "emailservice", "checkoutservice",
    "recommendationservice", "adservice", "redis-cart",
]

ONLINE_BOUTIQUE_REAL_EDGES: list[tuple[str, str]] = [
    ("frontend", "productcatalogservice"),
    ("frontend", "currencyservice"),
    ("frontend", "cartservice"),
    ("frontend", "checkoutservice"),
    ("frontend", "shippingservice"),
    ("frontend", "recommendationservice"),
    ("frontend", "adservice"),
    ("checkoutservice", "productcatalogservice"),
    ("checkoutservice", "cartservice"),
    ("checkoutservice", "currencyservice"),
    ("checkoutservice", "shippingservice"),
    ("checkoutservice", "paymentservice"),
    ("checkoutservice", "emailservice"),
    ("cartservice", "redis-cart"),
    ("recommendationservice", "productcatalogservice"),
]


# Port/protocol thật của Online Boutique (public, ổn định theo kiến trúc đã công bố của
# GoogleCloudPlatform/microservices-demo) — dùng để harness Deployment (script 05) biết
# gọi bằng công cụ nào: curl (HTTP), grpcurl (GRPC), hoặc bash /dev/tcp (TCP thuần).
SERVICE_PORTS: dict[str, tuple[int, str]] = {
    "frontend": (80, "HTTP"),
    "cartservice": (7070, "GRPC"),
    "productcatalogservice": (3550, "GRPC"),
    "currencyservice": (7000, "GRPC"),
    "paymentservice": (50051, "GRPC"),
    "shippingservice": (50051, "GRPC"),
    "emailservice": (5000, "GRPC"),
    "checkoutservice": (5050, "GRPC"),
    "recommendationservice": (8080, "GRPC"),
    "adservice": (9555, "GRPC"),
    "redis-cart": (6379, "TCP"),
}


class ScenarioKind(str, Enum):
    STRONG_HIDDEN = "STRONG_HIDDEN"
    SPARSE_HIDDEN = "SPARSE_HIDDEN"
    CONFIG_ONLY_CLEAN = "CONFIG_ONLY_CLEAN"
    CONFIG_ONLY_UNRESOLVED = "CONFIG_ONLY_UNRESOLVED"
    NOISE_HEALTHCHECK = "NOISE_HEALTHCHECK"
    NOISE_MONITORING = "NOISE_MONITORING"


@dataclass
class InjectedEdge:
    kind: ScenarioKind
    source: str
    destination: str
    is_real_dependency: bool  # ground truth
    # Tham số cho harness Deployment (script 05) — None nghĩa là "không áp dụng" (ví dụ
    # CONFIG_ONLY_* không sinh traffic nên không cần rate/path).
    requests_per_minute: Optional[int] = None
    active_days_out_of_window: Optional[int] = None  # mục 13.3: SPARSE <=2, STRONG ~ window_days
    path: Optional[str] = None
    declare_config_reference: bool = False  # có tạo ConfigMap/env trỏ tới destination không
    config_reference_resolvable: bool = True  # False = CONFIG_ONLY_UNRESOLVED (trỏ tên không tồn tại)

    def pair_key(self, namespace: str = NAMESPACE) -> PairKey:
        return (namespace, self.source, namespace, self.destination)

    @property
    def destination_port_protocol(self) -> tuple[int, str]:
        """Tra `SERVICE_PORTS` theo tên K8s Service thật của destination — luôn tra
        được vì mọi `destination` chọn từ `ONLINE_BOUTIQUE_SERVICES` (`_candidate_pairs`)."""
        return SERVICE_PORTS[self.destination]


@dataclass
class TrialScenario:
    trial_id: str
    seed: int
    injected_edges: list[InjectedEdge] = field(default_factory=list)

    def ground_truth(self, namespace: str = NAMESPACE) -> dict[PairKey, bool]:
        """Bao gồm CẢ cạnh thật nền (luôn True) LẪN cạnh vừa cấy — đúng mục 13.3
        (dùng cùng ground truth đã thiết kế, không tách rời cạnh nền và cạnh cấy)."""
        gt: dict[PairKey, bool] = {
            (namespace, s, namespace, d): True for s, d in ONLINE_BOUTIQUE_REAL_EDGES
        }
        for edge in self.injected_edges:
            gt[edge.pair_key(namespace)] = edge.is_real_dependency
        return gt


# Bán kính chọn cặp cấy: mọi cặp (source, dest) source != dest, KHÔNG trùng cạnh thật đã
# có sẵn (mục tiêu là cạnh "ẩn" — nếu trùng cạnh thật thì không kiểm chứng được gì mới).
def _candidate_pairs() -> list[tuple[str, str]]:
    real = set(ONLINE_BOUTIQUE_REAL_EDGES)
    pairs = []
    for s in ONLINE_BOUTIQUE_SERVICES:
        for d in ONLINE_BOUTIQUE_SERVICES:
            if s != d and (s, d) not in real:
                pairs.append((s, d))
    return pairs


_MONITORING_SOURCES = ("prometheus", "grafana", "blackbox-exporter")  # khớp config.py::monitoring_source_services
_HEALTHCHECK_PATHS = ("/health", "/healthz", "/readyz")


def generate_trial(trial_id: str, seed: Optional[int] = None) -> TrialScenario:
    """Sinh 1 kịch bản cấy cho 1 trial — REPRODUCIBLE theo `seed` (mặc định suy từ
    `trial_id` bằng hash ổn định, để chạy lại cùng trial_id luôn ra cùng kịch bản, tiện
    khi cần dựng lại hạ tầng sau khi cluster bị xóa giữa chừng).

    Mỗi trial gồm ĐỦ 6 loại (mỗi loại 1 cạnh) — không phải rút ngẫu nhiên số lượng/loại,
    để mọi trial đều đóng góp dữ liệu cho cả 2 lát cắt mục 13.3 (traffic thưa,
    HEALTHCHECK/MONITORING) thay vì phụ thuộc may rủi có trial nào "thiếu" 1 loại."""
    if seed is None:
        seed = abs(hash(trial_id)) % (2**32)
    rng = random.Random(seed)

    # NOISE_HEALTHCHECK BẮT BUỘC đích là "frontend" -- service HTTP DUY NHẤT của Online
    # Boutique (SERVICE_PORTS). classification.py chỉ nhận diện HEALTHCHECK qua
    # `series.path` (mục 2) -- path chỉ có ý nghĩa/quan sát được thật qua 1 request HTTP
    # thật. Nếu cấy cạnh này vào 1 dependency GRPC/TCP, harness (render_manifests.py)
    # không có cách nào tạo ra 1 request mang path đó mà không phá vỡ giao thức thật của
    # cổng đích (gRPC dùng HTTP/2 + framing riêng, TCP thuần không có path) -- traffic
    # thật sự thu được sẽ rơi vào BUSINESS/UNKNOWN chứ không phải HEALTHCHECK, làm hỏng
    # đúng lát cắt mà kịch bản này cần đo (mục 13.3: "B1 dự kiến precision thấp"). Vì vậy
    # tách riêng khỏi pool chung, không rút ngẫu nhiên đích như 5 cạnh còn lại.
    healthcheck_source = rng.choice([s for s in ONLINE_BOUTIQUE_SERVICES if s != "frontend"])

    # Pool cho 5 cạnh còn lại: loại bỏ mọi cặp có đích "frontend" (dành riêng ở trên) để
    # không trùng pair_key với cạnh NOISE_HEALTHCHECK.
    pool = [(s, d) for (s, d) in _candidate_pairs() if d != "frontend"]
    rng.shuffle(pool)
    if len(pool) < 5:
        raise ValueError("candidate pool quá nhỏ — cần >=5 cặp không trùng cạnh thật")
    chosen = pool[:5]

    edges = [
        InjectedEdge(
            kind=ScenarioKind.STRONG_HIDDEN,
            source=chosen[0][0], destination=chosen[0][1],
            is_real_dependency=True,
            requests_per_minute=rng.randint(20, 60),
            active_days_out_of_window=7,
            path="/hidden/strong-call",
            declare_config_reference=True,
        ),
        InjectedEdge(
            kind=ScenarioKind.SPARSE_HIDDEN,
            source=chosen[1][0], destination=chosen[1][1],
            is_real_dependency=True,
            requests_per_minute=rng.randint(1, 3),
            active_days_out_of_window=rng.choice([1, 2]),  # mục 13.3: distinct_days<=2
            path="/hidden/sparse-call",
            declare_config_reference=False,
        ),
        InjectedEdge(
            kind=ScenarioKind.CONFIG_ONLY_CLEAN,
            source=chosen[2][0], destination=chosen[2][1],
            is_real_dependency=True,  # dependency có thật (từng/sẽ dùng) nhưng không có traffic lúc quan sát
            declare_config_reference=True,
            config_reference_resolvable=True,
        ),
        InjectedEdge(
            kind=ScenarioKind.CONFIG_ONLY_UNRESOLVED,
            source=chosen[3][0], destination=chosen[3][1],
            is_real_dependency=False,  # biến môi trường trỏ tên không tồn tại — không phải dependency
            declare_config_reference=True,
            config_reference_resolvable=False,
        ),
        InjectedEdge(
            kind=ScenarioKind.NOISE_HEALTHCHECK,
            # Đích CỐ ĐỊNH là "frontend" (xem giải thích ở healthcheck_source phía trên) —
            # đảm bảo path health-check quan sát được là traffic HTTP thật, không phải
            # gRPC/TCP bị ép path giả.
            source=healthcheck_source, destination="frontend",
            is_real_dependency=False,
            requests_per_minute=rng.randint(30, 90),
            active_days_out_of_window=7,
            path=rng.choice(_HEALTHCHECK_PATHS),
            declare_config_reference=False,
        ),
        InjectedEdge(
            kind=ScenarioKind.NOISE_MONITORING,
            # Nguồn CỐ ĐỊNH là 1 service monitoring giả lập, không phải service Online Boutique
            # thật — khớp đúng cách classification.py nhận diện MONITORING (theo TÊN NGUỒN).
            # Không ràng buộc đích như HEALTHCHECK vì MONITORING chỉ được nhận diện qua
            # TÊN NGUỒN (classification.py), không phụ thuộc path/protocol đích.
            source=rng.choice(_MONITORING_SOURCES), destination=chosen[4][1],
            is_real_dependency=False,
            requests_per_minute=rng.randint(10, 30),
            active_days_out_of_window=7,
            path="/scrape",
            declare_config_reference=False,
        ),
    ]

    return TrialScenario(trial_id=trial_id, seed=seed, injected_edges=edges)


def generate_all_trials(n_trials: int = 15, base_seed: int = 20260101) -> list[TrialScenario]:
    """`base_seed` cố định (mặc định: ngày bắt đầu thí nghiệm dạng YYYYMMDD, tùy chỉnh
    được) để CẢ BỘ 15 trial reproducible cùng nhau, không chỉ từng trial riêng lẻ.

    `trial_id` dùng LUÔN làm tên namespace K8s (`render_manifests.py::render()`) nên
    PHẢI là RFC 1123 label hợp lệ: chỉ chữ thường/số/dấu gạch ngang, KHÔNG được chứa
    dấu gạch dưới (`kubectl` từ chối thẳng "trial_01" với lỗi "must be a lowercase
    RFC 1123 label"). Vì vậy dùng "-" chứ không phải "_" để phân tách."""
    return [generate_trial(f"trial-{i:02d}", seed=base_seed + i) for i in range(1, n_trials + 1)]


# ---------------------------------------------------------------------------
# Kiến trúc "dùng chung hạ tầng" — 1 Online Boutique thật + N "virtual trial" chia sẻ
# CÙNG 1 namespace K8s thật (thay vì N namespace riêng như generate_all_trials()).
# Giải quyết bài toán capacity xác nhận THẬT qua check_capacity.sh (2026-09-27, máy Bin):
# namespace riêng chỉ chạy nổi tối đa 4/15 trial đồng thời (giới hạn bởi RAM) vì phần
# chiếm tài nguyên lớn nhất là APP CONTAINER (Online Boutique thật x15 bản), không phải
# mesh proxy (ambient đã cắt gần hết phần đó, xem README "Sizing"). Hướng đã chọn cùng cô
# Dung: app chỉ chạy 1 LẦN, N virtual trial chỉ khác nhau ở TẬP CẠNH CẤY.
# ---------------------------------------------------------------------------


@dataclass
class SharedEdge:
    """1 harness Deployment THẬT trong namespace dùng chung — `owners` là danh sách
    virtual_trial_id đã "chọn" cạnh này. Bình thường mỗi cạnh chỉ có 1 owner (đúng tinh
    thần "90 cạnh không trùng nhau"), NGOẠI TRỪ NOISE_HEALTHCHECK (xem
    `generate_shared_trials`) — khi đó nhiều virtual trial CÙNG dùng 1 harness Deployment
    thật, traffic của nó là bằng chứng hợp lệ cho MỌI owner. Đây KHÔNG phải cơ chế mới:
    `TrialScenario.ground_truth()` đã coi 15 cạnh REAL của Online Boutique là bằng chứng
    dùng chung cho MỌI trial từ trước giờ — ở đây chỉ mở rộng nguyên tắc đó sang 1 loại
    cạnh cấy có pool nguồn quá nhỏ để chia hết cho N virtual trial."""

    edge: InjectedEdge
    owners: list[str] = field(default_factory=list)

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        return (self.edge.kind.value, self.edge.source, self.edge.destination)


@dataclass
class SharedInfraScenario:
    namespace: str  # 1 namespace K8s DUY NHẤT cho toàn bộ N virtual trial
    virtual_trial_ids: list[str]
    shared_edges: list[SharedEdge]

    def edges_for(self, virtual_trial_id: str) -> list[InjectedEdge]:
        return [se.edge for se in self.shared_edges if virtual_trial_id in se.owners]

    def ground_truth_for(self, virtual_trial_id: str) -> dict[PairKey, bool]:
        """Cùng hình dạng với `TrialScenario.ground_truth()` — 15 cạnh REAL (luôn True,
        dùng chung) + CHỈ những cạnh cấy mà virtual_trial_id này là owner (không phải
        toàn bộ 90 cạnh — cạnh của virtual trial khác không thuộc ground truth của
        trial này, xem NOTE trong `08_export_trial_data.py::export_shared_trials`)."""
        gt: dict[PairKey, bool] = {
            (self.namespace, s, self.namespace, d): True for s, d in ONLINE_BOUTIQUE_REAL_EDGES
        }
        for edge in self.edges_for(virtual_trial_id):
            gt[edge.pair_key(self.namespace)] = edge.is_real_dependency
        return gt

    def owner_map(self) -> dict[PairKey, list[str]]:
        """pair_key (dùng `self.namespace` cho cả 2 phía, đúng format IdentityKey/
        ConfigEvidenceRecord.pair_key()) -> danh sách virtual_trial_id sở hữu cạnh đó.
        Dùng ở `08_export_trial_data.py` để tách 1 lần thu thập traffic/config CHUNG
        (từ `self.namespace`) thành N `TrialInput` riêng — KHÔNG dùng nhãn Prometheus
        `injection_id` (đã xác nhận không resolve được qua waypoint ambient, xem
        `TRAFFIC_COLLECTION_PIPELINE.md` mục 3), chỉ cần so khớp pair_key thuần Python,
        đã có sẵn và đã test kỹ ở `evaluation.py`."""
        return {se.edge.pair_key(self.namespace): list(se.owners) for se in self.shared_edges}


def generate_shared_trials(
    n_trials: int = 15,
    base_seed: int = 20260101,
    namespace: str = "shared-eval",
) -> SharedInfraScenario:
    """Sinh kịch bản cho kiến trúc "dùng chung hạ tầng" — xem docstring nhóm dataclass
    ở trên. KHÁC `generate_all_trials()`: 5/6 vai trò cấy (mọi loại trừ NOISE_HEALTHCHECK)
    rút KHÔNG LẶP LẠI từ 1 pool DÙNG CHUNG cho toàn bộ n_trials (thay vì mỗi trial tự rút
    pool riêng, có thể trùng cạnh giữa các trial như `generate_trial()`) — bắt buộc, vì
    trong 1 namespace DUY NHẤT, 2 harness Deployment trùng (kind, source, destination) sẽ
    trùng TÊN (`render_manifests.py::_harness_deployment_yaml`), `kubectl apply` sẽ coi là
    CÙNG 1 Deployment thay vì tạo pod độc lập cho từng virtual trial.

    GIỚI HẠN THẬT PHÁT HIỆN KHI THIẾT KẾ — NOISE_HEALTHCHECK: đích LUÔN LÀ "frontend"
    (bắt buộc, xem lý do ở `test_noise_healthcheck_harness_sends_a_real_http_healthcheck_path`),
    nên pool NGUỒN khả dụng chỉ có `len(ONLINE_BOUTIQUE_SERVICES) - 1` = 10 service. Với
    n_trials=15 > 10, theo nguyên lý Dirichlet KHÔNG THỂ có 15 cạnh NOISE_HEALTHCHECK
    không trùng nhau. Xử lý: sau khi dùng hết 10 nguồn duy nhất (round-robin qua
    `healthcheck_sources[i % 10]`), các virtual trial dư ra DÙNG CHUNG 1 harness Deployment
    NOISE_HEALTHCHECK đã tồn tại (owners nhiều hơn 1, xem `SharedEdge`) — với n_trials=15
    cụ thể: 10 cạnh có 1 owner, 5 cạnh có 2 owner (10 + 5 = 15 lượt "chọn").

    PHÁT HIỆN THÊM lúc tự kiểm tra (KHÔNG phải lỗi, cơ chế `owners` ở trên xử lý đúng):
    NOISE_MONITORING cũng có thể trùng dù rút từ pool 85 cặp KHÔNG lặp — vì key thật của
    nó là (nguồn giả lập ∈ 3 tên monitoring cố định, ĐÍCH lấy từ `p_monitoring[1]`), tức
    chỉ dùng ĐÚNG 1 nửa cặp đã rút (bỏ qua `p_monitoring[0]`) — với 15 trial x tối đa 3
    nguồn x <=10 đích khả dĩ, xác suất trùng không nhỏ (thực đo với base_seed mặc định:
    15 lượt chọn -> 13 Deployment duy nhất, 2 cặp trùng). Không cần sửa vì cơ chế
    `owners` đã coi đây là bằng chứng dùng chung hợp lệ y hệt NOISE_HEALTHCHECK — chỉ ghi
    chú lại để không ai nhầm là bug khi thấy `len(shared_edges) < 6 * n_trials`.

    Raises ValueError nếu n_trials lớn tới mức 5 vai trò còn lại (pool ~85 cặp, xem
    `_candidate_pairs()`) cũng không đủ chia hết — khác NOISE_HEALTHCHECK, 5 vai trò này
    KHÔNG có cơ chế dùng chung (dùng chung sẽ làm mất tính "90 cạnh không trùng nhau" mà
    không có lý do kiến trúc bắt buộc như NOISE_HEALTHCHECK, nên thà báo lỗi sớm còn hơn
    âm thầm giảm số cạnh độc lập)."""
    virtual_trial_ids = [f"trial-{i:02d}" for i in range(1, n_trials + 1)]
    rng = random.Random(base_seed)

    shared_pool = [(s, d) for (s, d) in _candidate_pairs() if d != "frontend"]
    rng.shuffle(shared_pool)
    needed = 5 * n_trials
    if len(shared_pool) < needed:
        raise ValueError(
            f"generate_shared_trials: pool chỉ có {len(shared_pool)} cặp không trùng cạnh "
            f"thật, cần {needed} ({n_trials} trial x 5 vai trò không lặp: STRONG_HIDDEN/"
            f"SPARSE_HIDDEN/CONFIG_ONLY_CLEAN/CONFIG_ONLY_UNRESOLVED/NOISE_MONITORING) — "
            f"giảm n_trials hoặc mở rộng ONLINE_BOUTIQUE_SERVICES."
        )

    healthcheck_sources = [s for s in ONLINE_BOUTIQUE_SERVICES if s != "frontend"]
    rng.shuffle(healthcheck_sources)

    shared_edges: dict[tuple[str, str, str], SharedEdge] = {}

    def _add_edge(edge: InjectedEdge, owner: str) -> None:
        key = (edge.kind.value, edge.source, edge.destination)
        existing = shared_edges.get(key)
        if existing is None:
            shared_edges[key] = SharedEdge(edge=edge, owners=[owner])
        else:
            existing.owners.append(owner)

    for i, vtid in enumerate(virtual_trial_ids):
        base = i * 5
        p_strong, p_sparse, p_clean, p_unresolved, p_monitoring = shared_pool[base : base + 5]

        _add_edge(
            InjectedEdge(
                kind=ScenarioKind.STRONG_HIDDEN,
                source=p_strong[0], destination=p_strong[1],
                is_real_dependency=True,
                requests_per_minute=rng.randint(20, 60),
                active_days_out_of_window=7,
                path="/hidden/strong-call",
                declare_config_reference=True,
            ),
            vtid,
        )
        _add_edge(
            InjectedEdge(
                kind=ScenarioKind.SPARSE_HIDDEN,
                source=p_sparse[0], destination=p_sparse[1],
                is_real_dependency=True,
                requests_per_minute=rng.randint(1, 3),
                active_days_out_of_window=rng.choice([1, 2]),
                path="/hidden/sparse-call",
                declare_config_reference=False,
            ),
            vtid,
        )
        _add_edge(
            InjectedEdge(
                kind=ScenarioKind.CONFIG_ONLY_CLEAN,
                source=p_clean[0], destination=p_clean[1],
                is_real_dependency=True,
                declare_config_reference=True,
                config_reference_resolvable=True,
            ),
            vtid,
        )
        _add_edge(
            InjectedEdge(
                kind=ScenarioKind.CONFIG_ONLY_UNRESOLVED,
                source=p_unresolved[0], destination=p_unresolved[1],
                is_real_dependency=False,
                declare_config_reference=True,
                config_reference_resolvable=False,
            ),
            vtid,
        )
        _add_edge(
            InjectedEdge(
                kind=ScenarioKind.NOISE_MONITORING,
                source=rng.choice(_MONITORING_SOURCES), destination=p_monitoring[1],
                is_real_dependency=False,
                requests_per_minute=rng.randint(10, 30),
                active_days_out_of_window=7,
                path="/scrape",
                declare_config_reference=False,
            ),
            vtid,
        )

        hc_source = healthcheck_sources[i % len(healthcheck_sources)]
        _add_edge(
            InjectedEdge(
                kind=ScenarioKind.NOISE_HEALTHCHECK,
                source=hc_source, destination="frontend",
                is_real_dependency=False,
                requests_per_minute=rng.randint(30, 90),
                active_days_out_of_window=7,
                path=rng.choice(_HEALTHCHECK_PATHS),
                declare_config_reference=False,
            ),
            vtid,
        )

    return SharedInfraScenario(
        namespace=namespace,
        virtual_trial_ids=virtual_trial_ids,
        shared_edges=list(shared_edges.values()),
    )
