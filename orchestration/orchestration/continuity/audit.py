"""Kubernetes Audit Log -> thay đổi chính sách mạng (MIGRATION_PLANNER_DESIGN.md mục 2.3-2.4).

Đầu vào: file audit log của kube-apiserver (`--audit-log-path`, mỗi dòng 1 JSON
`audit.k8s.io/v1 Event`) hoặc 1 `EventList`. Chỉ giữ sự kiện:
  - stage `ResponseComplete` (1 request sinh nhiều stage -> chỉ đếm 1 lần);
  - verb create / update / patch / delete;
  - resource thuộc nhóm theo dõi (mặc định networkpolicies + authorizationpolicies của Istio);
  - request thành công (responseStatus.code < 300; thiếu code coi là thành công).

`source` = GITOPS nếu người thực hiện là controller GitOps đã khai (Argo CD / Flux mặc định),
ngược lại IMPERATIVE (kubectl tay, script...). `exception_label` lấy từ
`requestObject.metadata.labels["kltn.zt/exception-id"]` — chỉ có khi audit policy ghi ở mức
Request/RequestResponse; ở mức Metadata thì dựa vào tên policy (xem api/readers.py).

Ghép commit Git (`git_commit_sha`) chưa làm — điểm mở, MIGRATION_PLANNER_DESIGN.md mục 4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

EXCEPTION_LABEL = "kltn.zt/exception-id"

WATCHED_RESOURCES = {
    "networkpolicies": "NetworkPolicy",
    "authorizationpolicies": "AuthorizationPolicy",
}

DEFAULT_GITOPS_ACTORS = (
    "system:serviceaccount:argocd:",       # Argo CD application controller
    "system:serviceaccount:flux-system:",  # Flux kustomize/helm controller
)

_VERB_TO_CHANGE = {"create": "CREATE", "update": "UPDATE", "patch": "UPDATE", "delete": "DELETE"}


@dataclass(frozen=True)
class ConfigChange:
    audit_id: str
    resource_kind: str
    resource_namespace: str
    resource_name: str
    change_type: str
    source: str
    actor: str
    exception_label: Optional[str]
    changed_at: str


def iter_audit_events(lines: Iterable[str]) -> Iterator[dict]:
    """Đọc từng dòng JSON; dòng rỗng/hỏng bị bỏ qua (audit log có thể bị cắt ở dòng cuối khi
    đang xoay vòng file). Hỗ trợ cả 1 dòng là `EventList`."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == "EventList":
            yield from (e for e in obj.get("items", []) if isinstance(e, dict))
        elif isinstance(obj, dict):
            yield obj


def _object_meta(event: dict) -> dict:
    for key in ("requestObject", "responseObject"):
        obj = event.get(key)
        if isinstance(obj, dict) and isinstance(obj.get("metadata"), dict):
            return obj["metadata"]
    return {}


def classify_source(actor: str, gitops_actors: Iterable[str] = DEFAULT_GITOPS_ACTORS) -> str:
    return "GITOPS" if any(actor.startswith(prefix) for prefix in gitops_actors) else "IMPERATIVE"


def parse_audit_event(event: dict, *, watched: dict = WATCHED_RESOURCES,
                      gitops_actors: Iterable[str] = DEFAULT_GITOPS_ACTORS) -> Optional[ConfigChange]:
    if event.get("stage") != "ResponseComplete":
        return None
    change_type = _VERB_TO_CHANGE.get(event.get("verb", ""))
    ref = event.get("objectRef") or {}
    kind = watched.get(ref.get("resource", ""))
    if change_type is None or kind is None or ref.get("subresource"):
        return None
    code = (event.get("responseStatus") or {}).get("code")
    if code is not None and int(code) >= 300:
        return None
    meta = _object_meta(event)
    name = ref.get("name") or meta.get("name")
    namespace = ref.get("namespace") or meta.get("namespace")
    audit_id = event.get("auditID")
    if not (name and namespace and audit_id):
        return None
    actor = (event.get("user") or {}).get("username") or "unknown"
    labels = meta.get("labels") or {}
    return ConfigChange(
        audit_id=str(audit_id),
        resource_kind=kind,
        resource_namespace=namespace,
        resource_name=name,
        change_type=change_type,
        source=classify_source(actor, gitops_actors),
        actor=actor,
        exception_label=labels.get(EXCEPTION_LABEL),
        changed_at=event.get("stageTimestamp") or event.get("requestReceivedTimestamp") or "",
    )


def parse_audit_log(lines: Iterable[str], **kwargs) -> list[ConfigChange]:
    out = []
    for event in iter_audit_events(lines):
        change = parse_audit_event(event, **kwargs)
        if change is not None:
            out.append(change)
    return out
