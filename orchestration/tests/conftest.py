"""Fixture & factory dùng chung — cùng phong cách conftest của tier1_inference/tier2_agent
(factory hàm nhận tham số tuỳ chỉnh, không gọi cluster/Prometheus/LLM thật)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import orchestration  # noqa: F401  (sys.path cho tier1/tier2_agent/scenario_pool)
from orchestration.batch import BatchDeps, BatchSettings
from orchestration.db import open_db
from tier1.config_collector.k8s_types import ContainerSpec, FakeK8sReader, PodInfo, ServiceInfo, ServicePortInfo
from tier1.traffic_collector import (
    FakePrometheusClient,
    build_daily_bucket_query,
    build_error_query,
    build_http_grpc_query,
    build_scrape_gap_query,
    build_tcp_query,
)
from tier2_agent.models import RoundResult

NS = "shared-eval"
NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
DEPLOYED_AT = NOW - timedelta(days=8)

# Cạnh cấy dùng trong toàn bộ test — (kind, source, destination, owners)
INJECTED = [
    ("STRONG_HIDDEN", "adservice", "paymentservice", ["trial-01"]),
    ("SPARSE_HIDDEN", "emailservice", "shippingservice", ["trial-01"]),
    ("CONFIG_ONLY_CLEAN", "currencyservice", "cartservice", ["trial-01"]),
    ("CONFIG_ONLY_UNRESOLVED", "paymentservice", "adservice", ["trial-01"]),
    ("STRONG_HIDDEN", "shippingservice", "emailservice", ["trial-02"]),
]

SERVICE_PORTS = {
    "frontend": 80, "cartservice": 7070, "productcatalogservice": 3550, "currencyservice": 7000,
    "paymentservice": 50051, "shippingservice": 50051, "emailservice": 5000, "checkoutservice": 5050,
    "recommendationservice": 8080, "adservice": 9555,
}

# Traffic thật trong Prometheus: (source, dest, path, total_count, active day offsets)
TRAFFIC = [
    ("frontend", "cartservice", "/hipstershop.CartService/GetCart", 7000, range(7)),           # cạnh thật OB
    ("adservice", "paymentservice", "/hipstershop.PaymentService/Charge", 7000, range(7)),    # trial-01 STRONG
    ("emailservice", "shippingservice", "/hipstershop.ShippingService/GetQuote", 3, [0]),     # trial-01 SPARSE
    ("shippingservice", "emailservice", "/hipstershop.EmailService/Send", 7000, range(7)),    # trial-02 STRONG
]


def pk(src: str, dst: str) -> tuple:
    return (NS, src, NS, dst)


# ---------------------------------------------------------------------------
# File hạ tầng (ground truth + owner_map) — đúng định dạng 05_deploy_shared.sh sinh ra
# ---------------------------------------------------------------------------


@pytest.fixture
def infra_files(tmp_path):
    from scenario_pool import ONLINE_BOUTIQUE_REAL_EDGES

    gt_dir = tmp_path / "ground_truth"
    gt_dir.mkdir()
    for trial in ("trial-01", "trial-02"):
        gt = {"|".join(pk(s, d)): True for s, d in ONLINE_BOUTIQUE_REAL_EDGES}
        for kind, s, d, owners in INJECTED:
            if trial in owners:
                gt["|".join(pk(s, d))] = kind not in ("CONFIG_ONLY_UNRESOLVED",)
        (gt_dir / f"{trial}.json").write_text(json.dumps(gt))
    owner_map = {"|".join(pk(s, d)): owners for _k, s, d, owners in INJECTED}
    om_path = tmp_path / "shared-eval.owner_map.json"
    om_path.write_text(json.dumps(owner_map))
    return {"ground_truth_dir": gt_dir, "owner_map_path": om_path, "db_path": tmp_path / "kltn.sqlite3"}


# ---------------------------------------------------------------------------
# Prometheus giả — trả đúng shape `data.result` cho các PromQL mà collect_traffic sinh ra
# ---------------------------------------------------------------------------


def _metric(src: str, dst: str, path: str) -> dict:
    return {
        "source_workload_namespace": NS, "source_canonical_service": src,
        "source_principal": f"spiffe://cluster.local/ns/{NS}/sa/{src}",
        "destination_workload_namespace": NS, "destination_canonical_service": dst,
        "destination_principal": f"spiffe://cluster.local/ns/{NS}/sa/{dst}",
        "destination_service_name": dst, "destination_service_namespace": NS,
        "destination_port": str(SERVICE_PORTS[dst]), "request_protocol": "grpc", "request_path": path,
    }


def make_prometheus(traffic=TRAFFIC, now: datetime = NOW, window_days: int = 7, gap_targets=()):
    responses = {
        build_scrape_gap_query(): [{"metric": {"instance": t}, "value": [0, "0"]} for t in gap_targets],
        build_http_grpc_query(window_days, [NS], None): [
            {"metric": _metric(s, d, p), "value": [0, str(total)]} for s, d, p, total, _days in traffic
        ],
        build_error_query(window_days, [NS], None): [],
        build_tcp_query(window_days, [NS], None): [],
    }
    for offset in range(window_days):
        query, _iso = build_daily_bucket_query(offset, now, [NS], None)
        responses[query] = [
            {"metric": _metric(s, d, p), "value": [0, str(total / len(list(days)))]}
            for s, d, p, total, days in traffic if offset in list(days)
        ]
    return FakePrometheusClient(responses=responses)


# ---------------------------------------------------------------------------
# Cluster giả + LLM giả "trích chuẩn" (đọc đúng các <config_item> trong prompt)
# ---------------------------------------------------------------------------


_ITEM_RE = re.compile(r'<config_item id="(?P<id>i\d+)"[^>]*>\n(?P<value>.*?)\n</config_item>', re.S)
_HOSTPORT_RE = re.compile(r"^(?P<host>[a-z][a-z0-9.-]*)(?::(?P<port>\d+))?$")


class PerfectExtractionLLM:
    """Giả 1 LLM trích đúng mọi giá trị dạng `host` / `host:port` — đủ để chạy pipeline
    collector end-to-end mà không phải viết tay item_id cho từng chunk."""

    model_id = "fake-perfect"

    def __init__(self, fail_with: Exception | None = None):
        self.calls = 0
        self._fail_with = fail_with

    def complete(self, system_prompt: str, user_prompt: str) -> dict:
        self.calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        refs = []
        for m in _ITEM_RE.finditer(user_prompt):
            hp = _HOSTPORT_RE.match(m.group("value").strip())
            if hp:
                refs.append({"item_id": m.group("id"), "quote": m.group("value").strip(),
                             "host": hp.group("host"), "port": int(hp.group("port")) if hp.group("port") else None,
                             "scheme": None, "port_item_id": None, "port_quote": None})
        return {"references": refs}


def make_k8s_reader() -> FakeK8sReader:
    pods, services = [], []
    for svc, port in SERVICE_PORTS.items():
        pods.append(PodInfo(name=svc, namespace=NS, service_account_name=svc,
                            labels={"app": svc, "service.istio.io/canonical-name": svc},
                            containers=[ContainerSpec(name="server", env={"PORT": str(port)})]))
        services.append(ServiceInfo(name=svc, namespace=NS, selector={"app": svc},
                                    ports=[ServicePortInfo(port=port, target_port=port)]))
    for kind, src, dst, _owners in INJECTED:
        if kind not in ("CONFIG_ONLY_CLEAN", "CONFIG_ONLY_UNRESOLVED"):
            continue
        target = f"{dst}:{SERVICE_PORTS[dst]}" if kind == "CONFIG_ONLY_CLEAN" else f"{dst}-nonexistent"
        name = f"harness-{kind.lower().replace('_', '-')}-{src}-{dst}"
        pods.append(PodInfo(name=name, namespace=NS, service_account_name=src,
                            labels={"app": name, "service.istio.io/canonical-name": src, "test-harness": "true"},
                            containers=[ContainerSpec(name="harness", env={"HIDDEN_DEP_TARGET": target})]))
    return FakeK8sReader(pods=pods, services=services)


# ---------------------------------------------------------------------------
# Tier 2 giả — trả RoundResult theo kịch bản, ghi lại mọi lần gọi
# ---------------------------------------------------------------------------


class ScriptedTier2Runner:
    def __init__(self, final_status: str = "CONFIDENT", original_status: str | None = None,
                 by_destination: dict | None = None, fail_on_call: int | None = None):
        self.final_status = final_status
        self.original_status = original_status
        self.by_destination = by_destination or {}
        self.fail_on_call = fail_on_call
        self.calls: list[tuple[dict, str]] = []

    def run(self, escalation_dict: dict, session_id: str) -> RoundResult:
        self.calls.append((escalation_dict, session_id))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("Anthropic API 529 overloaded (giả lập)")
        status = self.by_destination.get(escalation_dict["destination_canonical_service"], self.final_status)
        return RoundResult(run_id=session_id, iteration=1, hypotheses=[], final_status=status,
                           original_agent_status=self.original_status or status, action="conclude",
                           stopped=True, stopped_by="model", stop_reason=None, persisted=True)


# ---------------------------------------------------------------------------
# Factory dựng BatchSettings/BatchDeps
# ---------------------------------------------------------------------------


@pytest.fixture
def make_settings(infra_files):
    def _make(run_id: str = "trial-01", **overrides) -> BatchSettings:
        base = dict(run_id=run_id, db_path=infra_files["db_path"], namespace=NS,
                    ground_truth_dir=infra_files["ground_truth_dir"],
                    owner_map_path=infra_files["owner_map_path"])
        base.update(overrides)
        return BatchSettings(**base)

    return _make


@pytest.fixture
def make_deps():
    def _make(**overrides) -> BatchDeps:
        base = dict(prometheus_client=make_prometheus(), k8s_reader=make_k8s_reader(),
                    deployment_clock=lambda _ns: DEPLOYED_AT, llm_client=PerfectExtractionLLM(),
                    tier2_runner=ScriptedTier2Runner(), now=lambda: NOW)
        base.update(overrides)
        return BatchDeps(**base)

    return _make


@pytest.fixture
def db(infra_files):
    conn = open_db(infra_files["db_path"])
    yield conn
    conn.close()


def read_rows(db_path: Path, sql: str, params=()) -> list:
    conn = open_db(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
