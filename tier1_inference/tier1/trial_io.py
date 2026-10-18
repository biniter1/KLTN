"""
Đọc file JSON trial do `infra/scripts/08_export_trial_data.py` xuất ra -> `TrialInput`.

Dùng chung cho `09_run_experiment.py` (đánh giá R=15) và `10_hysteresis_ablation.py`
(ablation 7 snapshot). Đọc được CẢ file cũ (xuất trước 15/10/2026) lẫn file mới
(`export_format: 2`):

  - file cũ: `port = 0` nghĩa là "không có nhãn port" -> đổi thành None (port_source
    "unknown"); không có `traffic_days` -> gộp cạnh quay về lấy max số ngày (cờ
    `legacy_days`, TIER1_DESIGN.md mục 2b) — muốn số đúng thì chạy lại 08_export.
  - file mới: có `traffic_days`, `port_source`, `flags`, `series_daily_counts`.
Trường không còn tồn tại trong dataclass (vd do bản cũ hơn ghi ra) bị bỏ qua.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path

from .experiment import TrialInput
from .schema import (
    ConfigEvidenceRecord,
    ConfigFinding,
    EvidenceRef,
    Scope,
    ServiceRef,
    TrafficClass,
    TrafficSeries,
)

_SERIES_FIELDS = {f.name for f in dataclasses.fields(TrafficSeries)}
_CONFIG_FIELDS = {f.name for f in dataclasses.fields(ConfigEvidenceRecord)}


def series_from_dict(d: dict) -> TrafficSeries:
    d = {k: v for k, v in d.items() if k in _SERIES_FIELDS}
    ref = d.pop("destination_service_ref", None)
    d["destination_service_ref"] = ServiceRef(**ref) if ref else None
    tc = d.pop("traffic_class", None)
    d["traffic_class"] = TrafficClass(tc) if tc else None
    if d.get("port") == 0:  # file cũ: 0 = thiếu nhãn destination_port
        d["port"] = None
    d.setdefault("port_source", "observed" if d.get("port") is not None else "unknown")
    d["flags"] = list(d.get("flags") or [])
    d["traffic_days"] = list(d.get("traffic_days") or [])
    return TrafficSeries(**d)


def config_from_dict(d: dict) -> ConfigEvidenceRecord:
    d = {k: v for k, v in d.items() if k in _CONFIG_FIELDS}
    d["config_finding"] = ConfigFinding(d["config_finding"])
    d["scope"] = Scope(d["scope"])
    ref = d.get("destination_service_ref")
    if isinstance(ref, dict):
        d["destination_service_ref"] = ServiceRef(**ref)
    if "evidence_refs" in d:
        d["evidence_refs"] = tuple(EvidenceRef(**r) if isinstance(r, dict) else r for r in d["evidence_refs"])
    if "flags" in d:
        d["flags"] = tuple(d["flags"])
    return ConfigEvidenceRecord(**d)


@dataclass
class LoadedTrial:
    trial: TrialInput
    daily_counts: list[dict[str, float]] = field(default_factory=list)  # cùng thứ tự traffic_series
    export_format: int = 1


def load_trial(path: str | Path) -> LoadedTrial:
    raw = json.loads(Path(path).read_text())
    series = [series_from_dict(s) for s in raw["traffic_series"]]
    trial = TrialInput(
        trial_id=raw["trial_id"],
        traffic_series=series,
        config_evidence=[config_from_dict(c) for c in raw["config_evidence"]],
        ground_truth={tuple(k.split("|")): bool(v) for k, v in raw["ground_truth"].items()},
    )
    daily = raw.get("series_daily_counts") or [{} for _ in series]
    return LoadedTrial(trial=trial, daily_counts=daily, export_format=int(raw.get("export_format", 1)))


def load_trial_input(path: str | Path) -> TrialInput:
    return load_trial(path).trial
