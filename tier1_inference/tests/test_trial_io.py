"""trial_io.py — đọc file trial cũ (trước 15/10) và mới."""

import json

from tier1.trial_io import load_trial


def _write(tmp_path, series, **extra):
    payload = {"trial_id": "t", "traffic_series": series, "config_evidence": [], "ground_truth": {"a|b|c|d": True}}
    payload.update(extra)
    p = tmp_path / "t.json"
    p.write_text(json.dumps(payload))
    return p


_S = {"source_namespace": "n", "source_canonical_service": "a", "source_service_account": "a",
      "source_workload_namespace": "n", "destination_namespace": "n", "destination_canonical_service": "b",
      "destination_service_account": "b", "destination_service_ref": {"service_name": "b", "service_namespace": "n"},
      "protocol": "GRPC", "port": 0, "path": "/x", "count": 5, "distinct_days": 2, "last_seen_days_ago": 0.0,
      "error_count": 0, "is_injected": False, "injection_id": None, "workload_revision": "latest",
      "traffic_class": None, "protocol_diversity_old_field": 1}


def test_legacy_file_port_zero_becomes_unknown(tmp_path):
    t = load_trial(_write(tmp_path, [_S]))
    s = t.trial.traffic_series[0]
    assert t.export_format == 1
    assert s.port is None and s.port_source == "unknown" and s.traffic_days == []
    assert t.daily_counts == [{}]


def test_new_format_keeps_days_flags_and_daily_counts(tmp_path):
    new = dict(_S, port=7070, port_source="observed_log", traffic_days=["2026-10-02"], flags=["probe_path"])
    t = load_trial(_write(tmp_path, [new], export_format=2, series_daily_counts=[{"2026-10-02": 5}]))
    s = t.trial.traffic_series[0]
    assert (s.port, s.port_source, s.traffic_days, s.flags) == (7070, "observed_log", ["2026-10-02"], ["probe_path"])
    assert t.daily_counts == [{"2026-10-02": 5}]
