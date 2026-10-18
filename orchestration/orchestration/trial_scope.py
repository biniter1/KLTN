"""Phạm vi 1 virtual trial trong hạ tầng DÙNG CHUNG (infra/README.md, `05_deploy_shared.sh`).

Cả 15 virtual trial chạy chung 1 namespace. Traffic/config thu từ namespace đó rồi được TÁCH
cho từng trial bằng `pair_key()` + `owner_map` — đúng luật đang dùng ở
`infra/scripts/08_export_trial_data.py::split_shared_collection` (không dùng nhãn Prometheus
`injection_id`, vì nhãn đó không resolve được qua waypoint ambient):

  1 series/config-evidence thuộc trial X  <=>  pair_key là cạnh THẬT của Online Boutique
                                              HOẶC X nằm trong owner_map[pair_key].
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PairKey = tuple  # (source_ns, source_svc, dest_ns, dest_svc)

# render_manifests.py::_harness_deployment_yaml — cạnh CONFIG_ONLY_UNRESOLVED khai env
# HIDDEN_DEP_TARGET="<destination>-nonexistent". Collector trả FOUND_UNRESOLVED với
# destination_* = None (mục 6) -> pair_key có 2 phần tử None, không bao giờ khớp owner_map.
# Khớp lại bằng raw_host theo đúng quy ước đặt tên này (xem `owns_config_evidence`).
UNRESOLVED_TARGET_SUFFIX = "-nonexistent"


def _parse_pair_key(key_str: str, what: str) -> PairKey:
    parts = tuple(key_str.split("|"))
    if len(parts) != 4:
        raise ValueError(f"{what} key không đúng định dạng 'ns|src|ns|dst': {key_str!r}")
    return parts


def load_ground_truth(path: Path) -> dict:
    raw = json.loads(Path(path).read_text())
    return {_parse_pair_key(k, "Ground truth"): bool(v) for k, v in raw.items()}


def load_owner_map(path: Path) -> dict:
    raw = json.loads(Path(path).read_text())
    return {_parse_pair_key(k, "Owner map"): list(v) for k, v in raw.items()}


def real_pair_keys(namespace: str) -> set:
    from scenario_pool import ONLINE_BOUTIQUE_REAL_EDGES

    return {(namespace, s, namespace, d) for s, d in ONLINE_BOUTIQUE_REAL_EDGES}


@dataclass
class TrialScope:
    run_id: str
    namespace: str
    ground_truth: dict
    owner_map: dict
    real_pairs: set = field(default_factory=set)

    def owns(self, pair_key: PairKey) -> bool:
        return tuple(pair_key) in self.real_pairs or self.run_id in self.owner_map.get(tuple(pair_key), [])

    @property
    def owned_injected_pairs(self) -> list:
        return [pk for pk, owners in self.owner_map.items() if self.run_id in owners]

    def filter_series(self, series: list) -> list:
        return [s for s in series if self.owns(s.identity_key().pair_key())]

    def owns_config_evidence(self, record) -> bool:
        """Như `owns()`, cộng 1 luật cho record FOUND_UNRESOLVED (destination = None):
        thuộc trial X nếu X sở hữu cạnh cấy (ns, src, ns, d) cùng nguồn và raw_host
        (nhãn DNS đầu tiên) == f"{d}-nonexistent". Thiếu luật này, kịch bản
        CONFIG_ONLY_UNRESOLVED (đúng loại cạnh cần escalate Tier 2) bị loại khỏi MỌI trial.
        Record FOUND_UNRESOLVED do chính Online Boutique khai (không phải harness) vẫn bị
        loại như trước — không thuộc trial nào."""
        if record.destination_canonical_service is not None:
            return self.owns(record.pair_key())
        host = (record.raw_host or "").split(".")[0]
        for src_ns, src_svc, _dst_ns, dst_svc in self.owned_injected_pairs:
            if (src_ns == record.source_namespace and src_svc == record.source_canonical_service
                    and host == f"{dst_svc}{UNRESOLVED_TARGET_SUFFIX}"):
                return True
        return False

    def filter_config_evidence(self, records: list) -> list:
        return [r for r in records if self.owns_config_evidence(r)]


def load_trial_scope(run_id: str, namespace: str, ground_truth_dir: Path, owner_map_path: Path) -> TrialScope:
    """Ném FileNotFoundError/ValueError với thông báo rõ — Orchestrator ghi thành
    `scenario_check` FAILED."""
    gt_path = Path(ground_truth_dir) / f"{run_id}.json"
    if not gt_path.exists():
        raise FileNotFoundError(
            f"không thấy ground truth '{gt_path}' — trial '{run_id}' chưa được 05_deploy_shared.sh sinh ra?"
        )
    if not Path(owner_map_path).exists():
        raise FileNotFoundError(f"không thấy owner_map '{owner_map_path}' (05_deploy_shared.sh ghi vào infra/.cache/)")
    scope = TrialScope(
        run_id=run_id,
        namespace=namespace,
        ground_truth=load_ground_truth(gt_path),
        owner_map=load_owner_map(Path(owner_map_path)),
        real_pairs=real_pair_keys(namespace),
    )
    if not scope.owned_injected_pairs:
        raise ValueError(
            f"owner_map không có cạnh cấy nào thuộc '{run_id}' — sai --run-id, sai --namespace, "
            f"hoặc owner_map của lần deploy khác?"
        )
    return scope
