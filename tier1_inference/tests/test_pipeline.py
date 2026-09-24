"""run_tier1() — test tích hợp end-to-end, độc lập với examples/simulate_data.py
(tự dựng dữ liệu tối giản cho từng nhánh) để test suite không phụ thuộc dữ liệu demo."""

from __future__ import annotations

from tier1.config import ScoringConfig
from tier1.pipeline import run_tier1
from tier1.schema import ConfigFinding, DependencyStatus, Scope, ServiceRef
from tier1.selector_validation import MockSelectorValidator
from tier1 import storage

from .conftest import make_config_evidence, make_series


def test_strong_vs_weak_relative_to_snapshot_volume(config):
    """Cùng 1 snapshot: edge traffic lớn -> STRONG, edge traffic nhỏ (so với percentile_95
    của chính snapshot đó) -> WEAK. Chứng minh log_volume thật sự tương đối theo mục 4,
    không phải một ngưỡng count tuyệt đối."""
    high = make_series(dest="productcatalogservice", path="/Business", count=1000,
                        distinct_days=7, last_seen_days_ago=0.1)
    low = make_series(dest="currencyservice", path="/Business", count=5,
                       distinct_days=1, last_seen_days_ago=3.0)

    results = run_tier1([high, low], [], snapshot_id="snap1", config=config)
    by_dest = {r.identity_key.destination_canonical_service: r for r in results}

    assert by_dest["productcatalogservice"].dependency_status == DependencyStatus.OBSERVED_STRONG
    assert by_dest["productcatalogservice"].escalate_to_tier2 is False

    assert by_dest["currencyservice"].dependency_status == DependencyStatus.OBSERVED_WEAK
    assert by_dest["currencyservice"].escalate_to_tier2 is True


def test_not_observed_clean_config_needs_human_review(config):
    ev = make_config_evidence(dest="emailservice", finding=ConfigFinding.FOUND_EMBEDDED)
    results = run_tier1([], [ev], snapshot_id="snap1", config=config)
    assert len(results) == 1
    r = results[0]
    assert r.dependency_status == DependencyStatus.NOT_OBSERVED
    assert r.needs_human_review is True
    assert r.escalate_to_tier2 is False
    assert r.count == 0


def test_not_observed_blind_escalates(config):
    ev = make_config_evidence(dest="shipping-backend", finding=ConfigFinding.FOUND_UNRESOLVED,
                               protocol=None, port=None, scope=Scope.UNKNOWN)
    results = run_tier1([], [ev], snapshot_id="snap1", config=config)
    r = results[0]
    assert r.dependency_status == DependencyStatus.NOT_OBSERVED
    assert r.needs_human_review is False
    assert r.escalate_to_tier2 is True


def test_dns_edge_is_auto_allowed_and_excluded_from_scoring(config):
    dns = make_series(dest="kube-dns", dest_ns="kube-system", protocol="DNS", port=53,
                       path=None, count=300)
    results = run_tier1([dns], [], snapshot_id="snap1", config=config)
    assert len(results) == 1
    r = results[0]
    assert r.dns_auto_allow is True
    assert r.score is None


def test_unknown_class_escalates(config):
    weird = make_series(dest="paymentservice", protocol="CUSTOM-RPC", path=None, count=40)
    results = run_tier1([weird], [], snapshot_id="snap1", config=config)
    r = results[0]
    assert r.dependency_status == DependencyStatus.UNKNOWN
    assert r.escalate_to_tier2 is True


def test_external_scope_strong_edge_skips_selector_validation(config):
    """mục 8: EXTERNAL sinh ServiceEntry, không phải NetworkPolicy — selector validation
    (mục 3b) không được chạy cho scope=EXTERNAL, dù validator được cấu hình fail tất cả."""
    external = make_series(
        dest="api.stripe.com", protocol="HTTP", port=443, path="/v1/charges",
        count=1000, distinct_days=6, last_seen_days_ago=0.2,
        destination_service_ref=None,  # không resolve được Service nội bộ -> EXTERNAL
    )
    class RejectAll:
        def validate(self, edge):
            from tier1.selector_validation import ValidationResult
            return ValidationResult(passed=False, reason="reject-all (test)")

    results = run_tier1([external], [], snapshot_id="snap1", config=config, selector_validator=RejectAll())
    r = results[0]
    assert r.scope == Scope.EXTERNAL
    assert r.dependency_status == DependencyStatus.OBSERVED_STRONG  # KHÔNG bị hạ xuống NEEDS_MANUAL_SELECTOR


def test_in_cluster_strong_edge_downgraded_when_selector_fails(config):
    strong = make_series(
        dest="redis-cart", protocol="TCP", port=6379, path=None, count=1000,
        distinct_days=7, last_seen_days_ago=0.05,
        destination_service_ref=ServiceRef("redis-cart", "default"),
    )
    validator = MockSelectorValidator(failing_services={"redis-cart"})
    results = run_tier1([strong], [], snapshot_id="snap1", config=config, selector_validator=validator)
    r = results[0]
    assert r.dependency_status == DependencyStatus.NEEDS_MANUAL_SELECTOR
    assert r.needs_human_review is True
    assert r.manual_selector_reason is not None


def test_hysteresis_across_two_runs_with_persisted_conn(tmp_path):
    """Kèm 1 edge 'anchor' traffic lớn, ỔN ĐỊNH ở cả 2 lần chạy — cần thiết vì EXPECTED_MAX
    (percentile_95, mục 4) tính TRÊN TOÀN SNAPSHOT: nếu chỉ có 1 edge duy nhất, log_volume
    của chính nó luôn tự = 1.0 (percentile của tập 1 phần tử = chính nó), nên count đổi thế
    nào cũng không thấy log_volume giảm. Có anchor mới mô phỏng đúng bối cảnh nhiều edge như
    pipeline thật (giống examples/simulate_data.py)."""
    cfg = ScoringConfig()  # theta_promote=0.68, theta_demote=0.58 mặc định
    conn = storage.connect(tmp_path / "t.sqlite3")
    validator = MockSelectorValidator()

    def anchor():
        return make_series(dest="checkoutservice", path="/Business/Anchor", count=5000,
                            distinct_days=7, last_seen_days_ago=0.05)

    # run1: traffic mạnh -> score ~0.856 (STRONG, xem tính toán bằng scoring.py trực tiếp)
    strong_series = make_series(dest="productcatalogservice", path="/Business", count=1000,
                                 distinct_days=7, last_seen_days_ago=0.1)
    r1 = run_tier1([strong_series, anchor()], [], "snap1", conn=conn, config=cfg, selector_validator=validator)
    r1_pcs = next(r for r in r1 if r.identity_key.destination_canonical_service == "productcatalogservice")
    assert r1_pcs.dependency_status == DependencyStatus.OBSERVED_STRONG
    score_run1 = r1_pcs.score

    # run2: traffic giảm mạnh -> score ~0.637, nằm trong vùng chết [0.58, 0.68)
    weaker_series = make_series(dest="productcatalogservice", path="/Business", count=80,
                                 distinct_days=2, last_seen_days_ago=1.5)
    r2 = run_tier1([weaker_series, anchor()], [], "snap2", conn=conn, config=cfg, selector_validator=validator)
    r2_pcs = next(r for r in r2 if r.identity_key.destination_canonical_service == "productcatalogservice")

    assert r2_pcs.previous_status == DependencyStatus.OBSERVED_STRONG
    assert r2_pcs.score < score_run1
    # Bài test chỉ có giá trị nếu score đã tụt xuống dưới theta_promote (thật sự vào "vùng chết"
    # hoặc thấp hơn) — nếu không, hysteresis không được exercised, nên assert luôn điều kiện này.
    assert cfg.theta_demote <= r2_pcs.score < cfg.theta_promote
    assert r2_pcs.dependency_status == DependencyStatus.OBSERVED_STRONG  # giữ nguyên nhờ hysteresis

    conn.close()


def test_config_evidence_boosts_score_for_observed_edge(config):
    """mục 4a: config evidence chỉ được cộng vào công thức khi count>0 (đã có traffic).
    So sánh cùng traffic, có vs không có config evidence khớp -> có phải điểm cao hơn."""
    series_a = make_series(dest="svc-with-config", path="/Business", count=50,
                            distinct_days=3, last_seen_days_ago=1.0)
    series_b = make_series(dest="svc-without-config", path="/Business", count=50,
                            distinct_days=3, last_seen_days_ago=1.0)
    ev = make_config_evidence(dest="svc-with-config", finding=ConfigFinding.FOUND_EMBEDDED)

    results = run_tier1([series_a, series_b], [ev], snapshot_id="snap1", config=config)
    by_dest = {r.identity_key.destination_canonical_service: r for r in results}

    assert by_dest["svc-with-config"].score > by_dest["svc-without-config"].score
    assert by_dest["svc-with-config"].config_finding == ConfigFinding.FOUND_EMBEDDED
    assert by_dest["svc-without-config"].config_finding is None


def test_count_must_be_positive_for_strong_even_with_config_evidence(config):
    """Luật cứng mục 4a: config evidence mạnh tới đâu cũng không tự kích hoạt STRONG khi
    count=0 — vì chưa từng có traffic thì cặp đó không đi qua nhánh observed/sigmoid,
    mà đi thẳng vào nhánh NOT_OBSERVED (không có field score)."""
    ev = make_config_evidence(dest="never-called", finding=ConfigFinding.FOUND_STRUCTURAL)
    results = run_tier1([], [ev], snapshot_id="snap1", config=config)
    r = results[0]
    assert r.count == 0
    assert r.score is None
    assert r.dependency_status != DependencyStatus.OBSERVED_STRONG
