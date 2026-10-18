"""
Config Evidence Collector v2 (LLM-assisted) — implement theo CONFIG_EVIDENCE_COLLECTOR.md.

Pipeline (mục 4.0, mục 10) — ĐÃ VIẾT ĐỦ 5 bước + lưu trữ:

    collect.py      [1] thu thập raw item theo workload (pod template)
    k8s_types.py        kiểu dữ liệu + KubernetesReader (duyệt đủ workload template, mục 3.1)
    image_config.py     đọc ENV/ENTRYPOINT/CMD của image (registry v2 / crictl node kind)
    merge.py        [2] hợp nhất thành effective config (precedence, $(VAR) expansion)
    funnel.py       [3] phễu lọc bảo thủ + chuẩn hoá + chunk
    extract.py      [4] LLM extraction, có cache
    identity.py     [5e] canonical_service + service_account (dùng chung bởi validate.py)
    validate.py     [5a-d, f-g] grounding, phân giải K8s/Istio, port, tự tham chiếu, dedup
    store.py            ghi 5 bảng SQLite mục 6 + SqliteExtractionCache
    pipeline.py         entry point `collect_config_evidence()` nối toàn bộ pipeline trên

`regex_baseline.py` là module v1 (parser rule-based) giữ lại làm baseline so sánh
(mục 9.2), KHÔNG còn là đường chính — xem docstring đầu file đó.

Giao diện với Tier 1 không đổi: xem `tier1.schema.ConfigEvidenceRecord`.

Re-export `collect_config_evidence`/`KubernetesReader` ở đây để
`infra/scripts/08_export_trial_data.py` không phải sửa câu import cũ
(`from tier1.config_collector import KubernetesReader, collect_config_evidence`).
"""

from .extract import AnthropicLLMClient, FakeLLMClient, GeminiLLMClient, GroqLLMClient, OpenAILLMClient
from .image_config import (
    ChainImageConfigReader,
    KindNodeImageConfigReader,
    RegistryImageConfigReader,
    default_image_reader,
)
from .k8s_types import CraneImageConfigReader, FakeK8sReader, KubernetesReader, NullImageConfigReader
from .pipeline import collect_config_evidence

__all__ = [
    "collect_config_evidence",
    "KubernetesReader",
    "FakeK8sReader",
    "CraneImageConfigReader",
    "RegistryImageConfigReader",
    "KindNodeImageConfigReader",
    "ChainImageConfigReader",
    "NullImageConfigReader",
    "default_image_reader",
    "AnthropicLLMClient",
    "GeminiLLMClient",
    "GroqLLMClient",
    "OpenAILLMClient",
    "FakeLLMClient",
]
