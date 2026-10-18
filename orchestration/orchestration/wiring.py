"""Nối các cài đặt THẬT (Prometheus, K8s, Anthropic, gatekeeper HTTP) cho Batch Orchestrator.

Tách khỏi `batch.py` để `batch.py` chỉ phụ thuộc interface — test tiêm Fake/Scripted, CLI
tiêm bản thật ở đây. Mọi API key đọc từ biến môi trường của shell chạy Orchestrator
(ORCHESTRATION_DESIGN.md mục 7: "API key của LLM chỉ nằm ở Batch Orchestrator").
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import _paths  # noqa: F401

ONLINE_BOUTIQUE_SERVICES = (
    "frontend", "cartservice", "productcatalogservice", "currencyservice", "paymentservice",
    "shippingservice", "emailservice", "checkoutservice", "recommendationservice", "adservice",
    "loadgenerator", "shoppingassistantservice",
)


# ---------------------------------------------------------------------------
# Thời điểm deploy (scenario_check)
# ---------------------------------------------------------------------------


def fixed_deployment_clock(deployed_at: datetime):
    """`--deployed-at` — người chạy tự khai thời điểm deploy."""
    return lambda _namespace: deployed_at


def kubernetes_deployment_clock(kubeconfig_path: Optional[str] = None,
                                label_selector: str = "test-harness=true"):  # pragma: no cover - cần cluster thật
    """Lúc harness pod ĐẦU TIÊN bắt đầu chạy (min `status.startTime` của pod mang nhãn
    `test-harness=true` — nhãn do `render_manifests.py` gắn). Đúng mốc mà infra/README.md
    và 08_export_trial_data.py dùng ("kể từ lúc harness pod đầu tiên Running")."""

    def _clock(namespace: str) -> Optional[datetime]:
        from kubernetes import client, config

        if kubeconfig_path:
            config.load_kube_config(config_file=kubeconfig_path)
        else:
            config.load_kube_config()
        pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=label_selector).items
        starts = [p.status.start_time for p in pods if p.status and p.status.start_time]
        return min(starts) if starts else None

    return _clock


# ---------------------------------------------------------------------------
# Tier 2 — traffic window provider + Brain runner
# ---------------------------------------------------------------------------


class PrometheusTrafficWindowProvider:
    """`TrafficProvider` cho tool `read_extended_traffic_window` của Tier 2: query Prometheus
    bằng ĐÚNG `collect_traffic()` của Tier 1 (cùng PromQL, cùng phân loại) với cửa sổ dài hơn,
    rồi gộp đúng 1 cạnh theo identity_key. Gọi thẳng Prometheus từ Orchestrator, không qua
    gatekeeper (README Tier 2 điểm mở 5 — xem README orchestration)."""

    def __init__(self, prometheus_client, namespaces: Optional[list] = None, now=None):
        self._client = prometheus_client
        self._namespaces = namespaces
        self._now = now  # callable -> datetime; None = giờ hiện tại (test tiêm giờ cố định)

    def fetch_extended_window(self, edge: str, window_days: int) -> dict:
        from tier1.aggregation import aggregate_series
        from tier1.classification import classify_all
        from tier1.config import ScoringConfig
        from tier1.traffic_collector import collect_traffic

        collection = collect_traffic(self._client, window_days=window_days, namespaces=self._namespaces,
                                     now=self._now() if self._now else None, check_gaps=False)
        matched = [s for s in collection.series if s.identity_key().as_string() == edge]
        if not matched:
            return {"count": 0, "distinct_days": 0, "error_ratio": 0.0, "protocol_diversity": 0}
        classify_all(matched, ScoringConfig())
        agg = next(iter(aggregate_series(matched).values()))
        return {
            "count": agg.count,
            "distinct_days": agg.distinct_days,
            "error_ratio": agg.error_ratio,
            "protocol_diversity": len(agg.distinct_protocols),
        }


class BrainTier2Runner:
    """Adapter `Tier2Runner` (batch.py) -> `tier2_agent.brain.Brain.run_session`."""

    def __init__(self, brain):
        self._brain = brain

    def run(self, escalation_dict: dict, session_id: str):
        from tier2_agent.escalation import EscalationRecord

        return self._brain.run_session(EscalationRecord.from_dict(escalation_dict), run_id=session_id)


def service_dirs_from_checkout(source_root: Path) -> dict:
    """canonical_service -> thư mục source trong bản checkout microservices-demo
    (`<root>/src/<service>`), chỉ giữ thư mục tồn tại thật."""
    root = Path(source_root)
    return {svc: str(root / "src" / svc) for svc in ONLINE_BOUTIQUE_SERVICES if (root / "src" / svc).is_dir()}


def build_brain_runner(
    *,
    db_path: Path,
    api_server_url: str,
    source_root: Path,
    prometheus_client,
    namespaces: Optional[list] = None,
    model_client=None,
) -> BrainTier2Runner:  # pragma: no cover - phần wiring thật, mỗi khối đã có test riêng ở tier2_agent/
    from tier2_agent.agent_runtime import AgentRuntime
    from tier2_agent.brain import Brain
    from tier2_agent.config import Tier2Config
    from tier2_agent.storage import Storage
    from tier2_agent.tool_registry import ToolRegistry
    from tier2_agent.tools.internal_docs import make_internal_docs_tool
    from tier2_agent.tools.pr_descriptions import (
        CachingGatekeeperClient,
        make_http_gatekeeper_fetch,
        make_pr_descriptions_tool,
    )
    from tier2_agent.tools.source_code import make_source_code_tool
    from tier2_agent.tools.traffic_window import make_traffic_window_tool

    config = Tier2Config()
    service_dirs = service_dirs_from_checkout(source_root)
    if not service_dirs:
        raise RuntimeError(f"không thấy thư mục src/<service> nào trong '{source_root}' — cần bản checkout "
                           f"GoogleCloudPlatform/microservices-demo cho tool read_source_code/read_internal_docs")
    # 1 tiến trình = 1 trial -> cache PR sống đúng 1 trial (TIER2_DESIGN.md mục 2), không cần reset_cache().
    gatekeeper = CachingGatekeeperClient(make_http_gatekeeper_fetch(api_server_url))
    registry = ToolRegistry(config, {
        "read_source_code": make_source_code_tool(service_dirs, stale_threshold_days=config.stale_threshold_days),
        "read_internal_docs": make_internal_docs_tool(service_dirs, stale_threshold_days=config.stale_threshold_days),
        "read_pr_descriptions": make_pr_descriptions_tool(gatekeeper, service_dirs),
        "read_extended_traffic_window": make_traffic_window_tool(
            PrometheusTrafficWindowProvider(prometheus_client, namespaces)),
    })
    if model_client is None:
        from tier2_agent.llm.client import AnthropicModelClient

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("thiếu biến môi trường ANTHROPIC_API_KEY cho Tier 2 — tự `export "
                               "ANTHROPIC_API_KEY=\"...\"` trong shell, hoặc chạy với --skip-tier2")
        model = os.environ.get("TIER2_MODEL")
        model_client = AnthropicModelClient(api_key=api_key, model=model) if model else AnthropicModelClient(api_key=api_key)
    brain = Brain(config, registry, Storage(str(db_path)), AgentRuntime(model_client))
    return BrainTier2Runner(brain)
