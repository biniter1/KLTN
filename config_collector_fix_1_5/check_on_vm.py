"""Kiểm tra CHỈ ĐỌC trên VM (không gọi LLM, không ghi gì, không đụng Prometheus):
in ra workload đã quét theo loại, các loại env đã đọc, và image nào đọc được config.

    cd tier1_inference
    python3 ../check_on_vm.py shared-eval            # 1 namespace
    python3 ../check_on_vm.py                        # mọi namespace
"""

import collections
import sys

from tier1.config_collector.collect import collect_raw_items
from tier1.config_collector.image_config import default_image_reader
from tier1.config_collector.k8s_types import KubernetesReader

ns = sys.argv[1] if len(sys.argv) > 1 else None
reader = KubernetesReader()
image_reader = default_image_reader()

pods = reader.list_pods(ns)
print(f"== {len(pods)} workload template ({ns or 'mọi namespace'})")
for kind, n in sorted(collections.Counter(p.workload_kind for p in pods).items()):
    print(f"   {kind:12s} {n}")
not_running = [p.name for p in pods if not p.is_running]
print(f"   không pod nào đang chạy (replicas=0 / CronJob chờ): {not_running or 'không có'}")

kinds = collections.Counter(e.kind for p in pods for c in p.containers for e in c.env_entries)
print(f"== env theo loại: {dict(kinds)}")

items = collect_raw_items(reader, image_reader, ns)
print(f"== {len(items)} raw item; theo nguồn: {dict(collections.Counter(it.source_kind for it in items))}")
for it in items:
    if it.source_ref and (it.source_ref.startswith("configmap:") and it.source_kind == "env"
                          or it.source_ref.startswith("downward:")):
        print(f"   {it.workload}/{it.container} {it.key} = {it.value!r}  [{it.source_ref}]")

if hasattr(image_reader, "resolved_refs"):
    print(f"== image đọc được config: {len(image_reader.resolved_refs)}")
    for r in sorted(image_reader.resolved_refs):
        print(f"   OK  {r}")
    for r in sorted(image_reader.unresolved_refs):
        print(f"   --  {r}")
