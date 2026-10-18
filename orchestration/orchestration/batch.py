"""Batch Orchestrator — tiến trình NGẮN HẠN, chạy đúng 1 trial rồi thoát (ORCHESTRATION_DESIGN.md mục 1).

Luồng 1 attempt (mỗi bước có RUNNING -> SUCCESS/FAILED trong `pipeline_run_log`):

    scenario_check     Hạ tầng dùng chung đã cấy cả 15 virtual trial MỘT LẦN và đang quan sát
                       chung (05_deploy_shared.sh) -> bước 1-2 của tài liệu ("cấy + chờ K ngày")
                       ở đây chỉ KIỂM TRA: ground truth + owner_map của trial có sẵn, và đã đủ
                       `window_days` kể từ lúc harness pod đầu tiên chạy. Chưa đủ -> FAILED.
    traffic_collector  Prometheus, 1 namespace chung -> tách theo pair_key cho trial này.
    config_collector   Config Evidence Collector v2 quét lại TỪ ĐẦU (cache LLM theo hash vẫn
                       dùng — mục 1 "cache không mâu thuẫn với quét lại"), tách theo pair_key.
                       Lỗi LLM -> FAILED, KHÔNG fallback regex (mục 2).
    tier1              run_tier1 STATELESS mặc định (giống tier1.experiment.run_trial — R=15
                       độc lập, không để trial này đọc previous_status của trial khác cùng
                       namespace). Ghi dependency_evidence gắn run_id + attempt_id.
    tier2              Mỗi cạnh Tier 1 escalate -> 1 phiên Brain riêng -> orchestrator_hook
                       UPDATE tier2_status đúng dòng của attempt này.

Luật cứng (mục 2): bất kỳ bước nào lỗi -> dòng `trial` FAILED + bước lỗi, dừng ngay. Dữ liệu
đã ghi một phần của attempt lỗi vẫn nằm trong bảng (để truy vết) nhưng KHÔNG BAO GIỜ lọt vào
view `dependency_current_state` (view chỉ lấy attempt có `trial` SUCCESS mới nhất).

Module này không có câu lệnh ghi nào vào `human_decisions` (bảng của API Server).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

from . import _paths  # noqa: F401
from .db import open_db
from .run_log import PipelineRunLog, StepFailed, next_attempt_id
from .trial_scope import TrialScope, load_trial_scope


class Tier2Runner(Protocol):
    def run(self, escalation_dict: dict, session_id: str):
        """Chạy 1 phiên Brain cho 1 cạnh, trả `tier2_agent.models.RoundResult`."""
        ...


DeploymentClock = Callable[[str], Optional[datetime]]
"""namespace -> thời điểm harness pod đầu tiên bắt đầu chạy (UTC), None nếu không xác định được."""


@dataclass
class BatchSettings:
    run_id: str
    db_path: Path
    namespace: str
    ground_truth_dir: Path
    owner_map_path: Path
    window_days: int = 7
    allow_short_window: bool = False   # dev/dry-run: bỏ qua kiểm tra "đã đủ window_days"
    fail_on_scrape_gap: bool = False   # mặc định: ghi cờ + cảnh báo (như 08_export), không hủy
    stateful_tier1: bool = False       # hysteresis theo lịch sử CHÍNH trial này (bộ demo)
    prompt_version: Optional[str] = None
    # tier1.config.ScoringConfig — None = EXPERIMENT_CONFIG (khai danh tính harness monitoring,
    # phương án a). Môi trường thật truyền DEFAULT_CONFIG hoặc config riêng.
    scoring_config: object = None


@dataclass
class BatchDeps:
    prometheus_client: object                  # tier1.traffic_collector.PrometheusClient
    k8s_reader: object                         # tier1.config_collector.k8s_types.K8sReader
    deployment_clock: DeploymentClock
    llm_client: object = None                  # None -> collector tự chọn theo env provider
    image_reader: object = None
    tier2_runner: Optional[Tier2Runner] = None  # None -> bỏ qua Tier 2 (ghi rõ skipped)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    # tier1.log_collector.LogsClient (VictoriaLogs) — None -> chỉ dùng metrics
    log_client: object = None
    # "ns/tên-service" của Prometheus/vmagent để tự phát hiện danh tính monitoring (TIER1_DESIGN.md mục 2.2)
    monitoring_services: tuple = ("istio-system/prometheus",)


@dataclass
class TrialOutcome:
    run_id: str
    attempt_id: str
    status: str                    # SUCCESS | FAILED
    failed_step: Optional[str] = None
    error_message: Optional[str] = None
    tier1_status_counts: dict = field(default_factory=dict)
    tier2_status_counts: dict = field(default_factory=dict)


def tier2_session_id(attempt_id: str, identity_key: str) -> str:
    """`agent_sessions.run_id` là PRIMARY KEY và 1 phiên = 1 cạnh, nên KHÔNG dùng thẳng run_id
    của trial (cạnh escalate thứ 2 sẽ trùng khoá). Id này trỏ ngược được về attempt + cạnh
    (DASHBOARD_DESIGN.md mục 4)."""
    digest = hashlib.sha1(identity_key.encode("utf-8")).hexdigest()[:12]
    return f"{attempt_id}#{digest}"


def escalation_dict(result) -> dict:
    """`DependencyEdgeResult` -> dict đúng schema mục 10 TIER1_DESIGN.md mà
    `tier2_agent.escalation.EscalationRecord.from_dict` nhận (không thêm/bớt field)."""
    ref = result.destination_service_ref
    data = dict(result.identity_key.as_dict())
    data.update({
        "destination_service_ref": (
            {"service_name": ref.service_name, "service_namespace": ref.service_namespace} if ref else None
        ),
        "scope": result.scope.value,
        "traffic_class": result.traffic_class.value,
        "count": result.count,
        "distinct_days": result.distinct_days,
        "error_ratio": result.error_ratio,
        "unstable": result.unstable,
        "config_finding": result.config_finding.value if result.config_finding else None,
        "score": result.score,
        "dependency_status": result.dependency_status.value,
        "snapshot_id": result.snapshot_id,
        "workload_revision": result.workload_revision,
    })
    return data


class BatchOrchestrator:
    def __init__(self, settings: BatchSettings, deps: BatchDeps):
        self.s = settings
        self.d = deps

    # ------------------------------------------------------------------ entry
    def run(self) -> TrialOutcome:
        conn = open_db(self.s.db_path)
        try:
            attempt_id = next_attempt_id(conn, self.s.run_id)
            log = PipelineRunLog(conn, self.s.run_id, attempt_id)
            outcome = TrialOutcome(self.s.run_id, attempt_id, "RUNNING")
            log.record("trial", "RUNNING", detail={"namespace": self.s.namespace,
                                                   "stateful_tier1": self.s.stateful_tier1})
            try:
                scope = self._scenario_check(log)
                series = self._collect_traffic(conn, log, scope, attempt_id)
                records = self._collect_config(conn, log, scope, attempt_id)
                results = self._run_tier1(conn, log, series, records, attempt_id, outcome)
                self._run_tier2(conn, log, results, attempt_id, outcome)
            except StepFailed as exc:
                outcome.status = "FAILED"
                outcome.failed_step = exc.step
                outcome.error_message = exc.message
                log.record("trial", "FAILED", error_message=f"bước {exc.step} lỗi: {exc.message}",
                           detail={"failed_step": exc.step})
                return outcome
            outcome.status = "SUCCESS"
            log.record("trial", "SUCCESS", detail={"tier1": outcome.tier1_status_counts,
                                                   "tier2": outcome.tier2_status_counts})
            return outcome
        finally:
            conn.close()

    # ------------------------------------------------------------------ steps
    def _scenario_check(self, log: PipelineRunLog) -> TrialScope:
        with log.step("scenario_check") as ctx:
            scope = load_trial_scope(self.s.run_id, self.s.namespace, self.s.ground_truth_dir, self.s.owner_map_path)
            ctx.detail["ground_truth_pairs"] = len(scope.ground_truth)
            ctx.detail["owned_injected_pairs"] = len(scope.owned_injected_pairs)

            deployed_at = self.d.deployment_clock(self.s.namespace)
            if deployed_at is None:
                raise StepFailed("scenario_check",
                                 f"không xác định được lúc harness pod đầu tiên chạy trong namespace "
                                 f"'{self.s.namespace}' (chưa deploy? hoặc truyền --deployed-at)")
            elapsed = self.d.now() - deployed_at
            ctx.detail["deployed_at"] = deployed_at.isoformat()
            ctx.detail["elapsed_days"] = round(elapsed.total_seconds() / 86400, 3)
            if elapsed < timedelta(days=self.s.window_days):
                if not self.s.allow_short_window:
                    raise StepFailed("scenario_check",
                                     f"mới quan sát {ctx.detail['elapsed_days']} ngày < window_days="
                                     f"{self.s.window_days} — chạy lại sau, hoặc --allow-short-window (chỉ để thử)")
                ctx.detail["short_window_allowed"] = True
            return scope

    def _collect_traffic(self, conn, log, scope: TrialScope, attempt_id: str) -> list:
        from tier1.config_collector import store as collector_store
        from tier1.traffic_collector import collect_traffic

        from tier1.cluster_context import build_cluster_context

        with log.step("traffic_collector") as ctx:
            # Cluster context (đọc 1 lần, chỉ đọc): suy port từ Service, path probe, danh tính
            # monitoring, host ServiceEntry — TIER1_DESIGN.md mục 2.1/2.2/8.
            context = build_cluster_context(self.d.k8s_reader, self.d.monitoring_services)
            collection = collect_traffic(self.d.prometheus_client, window_days=self.s.window_days,
                                         namespaces=[self.s.namespace], injected=None, now=self.d.now(),
                                         context=context, log_client=self.d.log_client)
            kept = scope.filter_series(collection.series)
            gap = collection.scrape_gap
            port_sources = Counter(s.port_source for s in kept)
            collector_store.insert_quality_log(conn, self.s.run_id, "traffic_collector", {
                "scrape_gap_detected": gap.has_gap,
                "scrape_gap_targets": list(gap.affected_targets),
                "series_total": len(collection.series),
                "series_kept_for_trial": len(kept),
                "series_port_source": dict(port_sources),
                "series_from_logs": collection.log_series_added,
                "log_rows_read": collection.log_rows_read,
            }, attempt_id=attempt_id)
            ctx.detail.update(series_total=len(collection.series), series_kept=len(kept),
                              scrape_gap_detected=gap.has_gap)
            if gap.has_gap and self.s.fail_on_scrape_gap:
                raise StepFailed("traffic_collector", f"scrape gap tại {gap.affected_targets}")
            return kept

    def _collect_config(self, conn, log, scope: TrialScope, attempt_id: str) -> list:
        from tier1.config_collector import store as collector_store
        from tier1.config_collector.pipeline import collect_config_evidence

        with log.step("config_collector") as ctx:
            stats: dict = {}
            kwargs = {}
            if self.s.prompt_version:
                kwargs["prompt_version"] = self.s.prompt_version
            # run_id của 5 bảng collector = attempt_id: mỗi lần chạy lại trial là 1 nhóm dòng
            # riêng, không trộn với lần chạy trước (bảng collector không có cột attempt).
            records = collect_config_evidence(
                self.d.k8s_reader, snapshot_id=attempt_id, namespaces=[self.s.namespace],
                image_reader=self.d.image_reader, llm_client=self.d.llm_client,
                run_id=attempt_id, db_path=self.s.db_path, attempt_id=attempt_id,
                write_quality_log=False, stats_out=stats, **kwargs,
            )
            kept = scope.filter_config_evidence(records)
            stats["evidence_kept_for_trial"] = len(kept)
            collector_store.insert_quality_log(conn, self.s.run_id, "config_collector", stats, attempt_id=attempt_id)
            ctx.detail.update(evidence_total=len(records), evidence_kept=len(kept),
                              llm_calls=stats.get("llm_calls"), llm_cache_hits=stats.get("llm_cache_hits"))
            return kept

    def _run_tier1(self, conn, log, series, records, attempt_id: str, outcome: TrialOutcome) -> list:
        from tier1 import storage as tier1_storage
        from tier1.config import EXPERIMENT_CONFIG
        from tier1.pipeline import run_tier1

        with log.step("tier1") as ctx:
            history_conn = self._lineage_history_conn(conn) if self.s.stateful_tier1 else None
            try:
                # snapshot_id = attempt_id: lịch sử loại đúng dòng của CHÍNH lần chạy này
                # (TIER1_DESIGN.md mục 10b); mỗi attempt là 1 snapshot riêng.
                results = run_tier1(series, records, snapshot_id=attempt_id, conn=history_conn,
                                    config=self.s.scoring_config or EXPERIMENT_CONFIG)
            finally:
                if history_conn is not None:
                    history_conn.close()
            tier1_storage.insert_results(conn, results, run_id=self.s.run_id, attempt_id=attempt_id)
            counts = Counter(r.dependency_status.value for r in results)
            outcome.tier1_status_counts = dict(counts)
            ctx.detail.update(edges=len(results), status_counts=dict(counts),
                              escalated=sum(1 for r in results if r.escalate_to_tier2),
                              needs_human_review=sum(1 for r in results if r.needs_human_review))
            return results

    def _run_tier2(self, conn, log, results, attempt_id: str, outcome: TrialOutcome) -> None:
        from tier2_agent.escalation import EscalationRecord
        from tier2_agent.orchestrator_hook import apply_tier2_result

        escalated = [r for r in results if r.escalate_to_tier2]
        with log.step("tier2") as ctx:
            ctx.detail["escalated"] = len(escalated)
            if self.d.tier2_runner is None:
                ctx.detail["skipped"] = True
                ctx.detail["reason"] = "không cấu hình Tier 2 (--skip-tier2) — cạnh escalate giữ nguyên trạng thái Tier 1"
                return
            counts: Counter = Counter()
            for r in escalated:
                esc_dict = escalation_dict(r)
                record = EscalationRecord.from_dict(esc_dict)
                session_id = tier2_session_id(attempt_id, record.identity_key)
                ctx.detail["current_session"] = session_id
                result = self.d.tier2_runner.run(esc_dict, session_id)
                affected = apply_tier2_result(conn, record, self.s.run_id, result,
                                              attempt_id=attempt_id, tier2_session_id=session_id)
                if affected != 1:
                    raise StepFailed("tier2", f"orchestrator_hook cập nhật {affected} dòng (kỳ vọng 1) cho "
                                              f"{record.identity_key}")
                counts[result.final_status] += 1
            ctx.detail.pop("current_session", None)
            ctx.detail["final_status_counts"] = dict(counts)
            outcome.tier2_status_counts = dict(counts)

    # ------------------------------------------------------------------ helpers
    def _lineage_history_conn(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        """--stateful: hysteresis (TIER1_DESIGN.md mục 5b) chỉ được nhìn lịch sử của CHÍNH
        trial này (các attempt SUCCESS trước), không nhìn trial khác cùng namespace. `run_tier1`
        đọc previous_status từ bảng không lọc run_id, nên chép đúng lịch sử đó sang 1 DB tạm
        trong RAM rồi đưa cho Tier 1 — không phải sửa code Tier 1."""
        from tier1 import storage as tier1_storage

        mem = tier1_storage.connect(":memory:")
        mem_cols = {row[1] for row in mem.execute("PRAGMA table_info(dependency_evidence)")}
        src_cols = [row[1] for row in conn.execute("PRAGMA table_info(dependency_evidence)")]
        cols = [c for c in src_cols if c in mem_cols and c != "id"]
        col_sql = ", ".join(cols)
        rows = conn.execute(
            f"SELECT {col_sql} FROM dependency_evidence WHERE run_id = ? AND attempt_id IN ("
            f"  SELECT attempt_id FROM pipeline_run_log WHERE run_id = ? AND step = 'trial' AND status = 'SUCCESS'"
            f") ORDER BY id",
            (self.s.run_id, self.s.run_id),
        ).fetchall()
        mem.executemany(
            f"INSERT INTO dependency_evidence ({col_sql}) VALUES ({', '.join('?' for _ in cols)})",
            [tuple(r) for r in rows],
        )
        mem.commit()
        return mem
