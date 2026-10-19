"""Rollout history and the rollback precondition, without a Kubernetes client.

``rollback_deployment`` used to run ``kubectl rollout undo``: roll back to
"the previous revision" as of whenever the command happens to run. Revision
numbers renumber on every rollback (the restored ReplicaSet takes the next
number), so "previous" is relative to the last write — a retry, a second
approval or an earlier restart each move it, and undo toggles straight back
onto the faulty version.

A rollback is therefore named by the target ReplicaSet's ``pod-template-hash``
(which never changes) together with the hash of the template it expects to
replace. This module holds the pure half of that contract so it can be tested
without a cluster: it reads the apiserver's raw JSON for one Deployment and its
ReplicaSets, and decides whether a bound rollback is already done, may be
applied, or meets a deployment that has moved since the approver saw it.
"""

import copy
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

POD_TEMPLATE_HASH_LABEL = "pod-template-hash"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

# A pod-template-hash is a label value the deployment controller derives from
# the template: short, lower-case alphanumeric.
TEMPLATE_HASH_RE = re.compile(r"^[a-z0-9]{1,63}$")

ALREADY_AT_TARGET = "already_at_target"
APPLY = "apply"
DRIFT = "drift"
TARGET_MISSING = "target_missing"
PAUSED = "paused"


def _meta(obj: Dict[str, Any]) -> Dict[str, Any]:
    return (obj or {}).get("metadata") or {}


def _template(obj: Dict[str, Any]) -> Dict[str, Any]:
    return ((obj or {}).get("spec") or {}).get("template") or {}


def _prune(value: Any) -> Any:
    """Drop empty values, the way the apiserver's semantic equality ignores them.

    Kubernetes compares templates treating a nil and an empty map or list as
    equal; raw JSON may carry either, depending on who wrote the object.
    """
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if v not in (None, {}, [])}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def template_without_hash(template: Dict[str, Any]) -> Dict[str, Any]:
    """The template as a Deployment carries it: minus the controller's hash label."""
    out = copy.deepcopy(template or {})
    labels = (out.get("metadata") or {}).get("labels")
    if isinstance(labels, dict):
        labels.pop(POD_TEMPLATE_HASH_LABEL, None)
    return out


def _identity(template: Dict[str, Any]) -> str:
    return json.dumps(_prune(template_without_hash(template)), sort_keys=True, separators=(",", ":"))


def template_fingerprint(template: Dict[str, Any]) -> str:
    """What the template runs, ignoring a rolling restart's timestamp.

    ``kubectl rollout restart`` only stamps ``restartedAt`` on the template,
    which makes a new revision that runs exactly what the previous one ran. Two
    revisions with one fingerprint are the same software, so rolling back from
    one to the other changes nothing — that is how a restart made after a bad
    deploy sent ``rollout undo`` straight back onto the bad deploy.
    """
    stripped = template_without_hash(template)
    annotations = (stripped.get("metadata") or {}).get("annotations")
    if isinstance(annotations, dict):
        annotations.pop(RESTARTED_AT_ANNOTATION, None)
    canonical = json.dumps(_prune(stripped), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _template_hash(replica_set: Dict[str, Any]) -> Optional[str]:
    labels = _meta(_template(replica_set)).get("labels") or {}
    value = labels.get(POD_TEMPLATE_HASH_LABEL) or (
        _meta(replica_set).get("labels") or {}
    ).get(POD_TEMPLATE_HASH_LABEL)
    return str(value) if value else None


def _revision(obj: Dict[str, Any]) -> Optional[int]:
    raw = (_meta(obj).get("annotations") or {}).get(REVISION_ANNOTATION)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _images(template: Dict[str, Any]) -> Dict[str, str]:
    containers = ((template or {}).get("spec") or {}).get("containers") or []
    return {str(c.get("name") or ""): str(c.get("image") or "") for c in containers}


def _owned_by(replica_set: Dict[str, Any], deployment: Dict[str, Any]) -> bool:
    """True only for a ReplicaSet this Deployment controls.

    A label selector is not ownership: two Deployments with overlapping
    selectors would otherwise offer each other's templates as rollback targets.
    """
    dep_meta = _meta(deployment)
    uid = dep_meta.get("uid")
    for ref in _meta(replica_set).get("ownerReferences") or []:
        if ref.get("kind") != "Deployment" or not ref.get("controller"):
            continue
        if uid and ref.get("uid"):
            if ref.get("uid") == uid:
                return True
        elif ref.get("name") == dep_meta.get("name"):
            return True
    return False


def owned_replica_sets(
    deployment: Dict[str, Any], replica_sets: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """This Deployment's ReplicaSets that carry a template hash, oldest first."""
    owned = [
        rs for rs in replica_sets or [] if _owned_by(rs, deployment) and _template_hash(rs)
    ]
    owned.sort(
        key=lambda rs: (
            str(_meta(rs).get("creationTimestamp") or ""),
            str(_meta(rs).get("name") or ""),
        )
    )
    return owned


def find_replica_set(
    deployment: Dict[str, Any], replica_sets: List[Dict[str, Any]], template_hash: str
) -> Optional[Dict[str, Any]]:
    for rs in owned_replica_sets(deployment, replica_sets):
        if _template_hash(rs) == template_hash:
            return rs
    return None


def current_replica_set(
    deployment: Dict[str, Any], replica_sets: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The ReplicaSet whose template the Deployment currently asks for.

    The same rule as the deployment controller's FindNewReplicaSet: template
    equality ignoring the hash label, oldest match wins. None means the
    template matches no ReplicaSet yet — the controller has not caught up with
    a change — which callers must treat as unknown, never as a match.
    """
    wanted = _identity(_template(deployment))
    for rs in owned_replica_sets(deployment, replica_sets):
        if _identity(_template(rs)) == wanted:
            return rs
    return None


def rollout_state(
    deployment: Dict[str, Any], replica_sets: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """A Deployment's revision history as plain data, newest revision first."""
    meta = _meta(deployment)
    spec = (deployment or {}).get("spec") or {}
    status = (deployment or {}).get("status") or {}
    current = current_replica_set(deployment, replica_sets)
    current_hash = _template_hash(current) if current else None
    revisions = []
    for rs in owned_replica_sets(deployment, replica_sets):
        template = _template(rs)
        revisions.append(
            {
                "template_hash": _template_hash(rs),
                "revision": _revision(rs),
                "created_at": _meta(rs).get("creationTimestamp"),
                "images": _images(template),
                "fingerprint": template_fingerprint(template),
                "replicas": int(((rs.get("status") or {}).get("replicas")) or 0),
                "current": _template_hash(rs) == current_hash,
            }
        )
    revisions.sort(key=lambda r: (r["revision"] is None, -(r["revision"] or 0)))
    return {
        "deployment": meta.get("name"),
        "namespace": meta.get("namespace"),
        "resource_version": meta.get("resourceVersion"),
        "generation": meta.get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "paused": bool(spec.get("paused")),
        "current_template_hash": current_hash,
        "current_revision": _revision(current) if current else None,
        "current_images": _images(_template(deployment)),
        "current_fingerprint": template_fingerprint(_template(deployment)),
        "revisions": revisions,
    }


def classify_rollback(
    deployment: Dict[str, Any],
    replica_sets: List[Dict[str, Any]],
    to_template_hash: str,
    expected_template_hash: str,
) -> Tuple[str, str]:
    """Decide what a bound rollback may do to the Deployment as it is now.

    Returns ``(verdict, reason)``: already at the target (nothing to write),
    on the expected template (apply), or anything else — drift, a target
    ReplicaSet that history no longer holds, or a paused rollout — which must
    be refused for a human, because it is not the state that was approved.
    """
    target = find_replica_set(deployment, replica_sets, to_template_hash)
    if target is None:
        return TARGET_MISSING, (
            f"no ReplicaSet with pod-template-hash {to_template_hash} belongs to "
            "this deployment (revision history may have pruned it)"
        )
    current = _identity(_template(deployment))
    if current == _identity(_template(target)):
        return ALREADY_AT_TARGET, f"already on template {to_template_hash}"
    if ((deployment or {}).get("spec") or {}).get("paused"):
        return PAUSED, "the deployment's rollout is paused; a template change would not roll out"
    expected = find_replica_set(deployment, replica_sets, expected_template_hash)
    if expected is not None and current == _identity(_template(expected)):
        return APPLY, f"on the expected template {expected_template_hash}"
    live = current_replica_set(deployment, replica_sets)
    live_hash = _template_hash(live) if live else None
    seen = f"template {live_hash}" if live_hash else "a template no ReplicaSet matches yet"
    return DRIFT, (
        f"expected template {expected_template_hash} but the deployment is on {seen}"
    )


def rollback_patch(
    deployment: Dict[str, Any], target_replica_set: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """A JSON patch that restores the target template only if nothing moved.

    The ``test`` on ``resourceVersion`` makes the write a compare-and-swap
    against the exact object the precondition was checked on. The template is
    the ReplicaSet's own, minus the hash label — what ``rollout undo
    --to-revision`` writes, without the revision number it would resolve.
    """
    return [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": _meta(deployment).get("resourceVersion"),
        },
        {
            "op": "replace",
            "path": "/spec/template",
            "value": template_without_hash(_template(target_replica_set)),
        },
    ]


def rollback_command(
    name: str, namespace: str, to_template_hash: str, expected_template_hash: str
) -> str:
    """The rollback as one line, identical on both sides of the approval.

    The agent renders this same string for the approver
    (``executor.build_command``); a test holds the two together. It is not a
    kubectl command: kubectl has no hash-addressed, preconditioned rollback.
    """
    return (
        f"sentinel rollback deployment/{name} -n {namespace} "
        f"--to-template={to_template_hash} --expect-template={expected_template_hash}"
    )


def selector_string(deployment: Dict[str, Any]) -> str:
    """The Deployment's matchLabels as a label selector, to narrow the RS list.

    Narrowing only: ownership is still checked per ReplicaSet, so an empty or
    expression-only selector just lists more and filters the same.
    """
    match = (((deployment or {}).get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(match.items()))
