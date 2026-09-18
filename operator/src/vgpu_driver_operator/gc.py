"""Garbage collection of stale vGPU driver images.

Determines which registry tags are safe to delete based on the retention
policy in the CRD spec, then deletes them via the registry API.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable

from vgpu_driver_operator import reconciler, registry as _registry
from vgpu_driver_operator.registry import RegistryAuth, TagDeletionDisabled

# Maximum pruned-history entries kept in status.
_MAX_PRUNED_HISTORY = 100


# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------


def parse_duration(s: str) -> timedelta:
    """Parse a simple duration string into a :class:`~datetime.timedelta`.

    Supported suffixes:
    - ``d``  → days
    - ``h``  → hours
    - ``m``  → minutes
    - ``s``  → seconds

    A plain integer (no suffix) is interpreted as seconds.

    Raises :class:`ValueError` on unrecognised format.
    """
    s = s.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([dhms]?)", s)
    if not m:
        raise ValueError(f"Cannot parse duration: {s!r}")
    value = float(m.group(1))
    unit = m.group(2) or "s"
    if unit == "d":
        return timedelta(days=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "s":
        return timedelta(seconds=value)
    raise ValueError(f"Unknown duration unit: {unit!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Main GC entry point
# ---------------------------------------------------------------------------


def repositories(spec: dict) -> list[str]:
    cfg = spec.get("registry") or {}
    repos = [cfg.get("repository", "")]
    if spec.get("precompile") or cfg.get("repositoryPrecompiled"):
        repos.append(cfg.get("repositoryPrecompiled") or cfg.get("repository", ""))
    return sorted(set(filter(None, repos)))


def parse_image_tag(tag: str) -> reconciler.BuildKey | None:
    """Strict retention parser; unrelated tags must never become candidates."""
    match = re.fullmatch(
        r"(?P<driver>\d+\.\d+(?:\.\d+)?(?:-[A-Za-z][A-Za-z0-9.]*)?)"
        r"(?:-(?P<kernel>\d+\.\d+\.[A-Za-z0-9_.-]+))?"
        r"-flatcar(?P<flatcar>\d+\.\d+\.\d+)",
        tag,
    )
    if match is None:
        return None
    return reconciler.BuildKey(
        match["driver"],
        match["flatcar"],
        precompile=match["kernel"] is not None,
    )


def retention_status(
    status: dict,
    now: datetime,
    *,
    result: str,
    reason: str,
    message: str,
    attempted: bool = False,
    candidate: int = 0,
    deleted: int = 0,
    skipped: int = 0,
) -> dict:
    previous = status.get("retention") or {}
    record = {
        key: previous[key] for key in ("lastAttemptTime", "lastSuccessTime") if key in previous
    }
    record.update(
        result=result,
        reason=reason,
        message=message,
        candidateCount=candidate,
        deletedCount=deleted,
        skippedCount=skipped,
    )
    if attempted:
        record["lastAttemptTime"] = now.isoformat()
    if result == "Succeeded":
        record["lastSuccessTime"] = now.isoformat()
    return record


def retention_condition(record: dict, status: dict, now: datetime) -> dict:
    healthy = record["result"] == "Succeeded" or record["reason"] == "Disabled"
    condition = {
        "type": "RetentionHealthy",
        "status": "True" if healthy else "False",
        "reason": record["reason"],
        "message": record["message"],
        "lastTransitionTime": now.isoformat(),
    }
    for old in status.get("conditions") or []:
        if old.get("type") == "RetentionHealthy" and old.get("status") == condition["status"]:
            condition["lastTransitionTime"] = old.get("lastTransitionTime", now.isoformat())
    return condition


def run(
    spec: dict,
    status: dict,
    *,
    auth: RegistryAuth | None,
    now: datetime,
    logger,
    emit_event: Callable[..., None],
    auth_by_repository: dict[str, RegistryAuth | None] | None = None,
) -> dict:
    """Plan from complete inventories, revalidate, then delete selected digests."""
    previous = list(status.get("pruned") or [])[:_MAX_PRUNED_HISTORY]
    result = {"pruned": previous}
    new_pruned: list[dict] = []
    candidate_count = skipped_count = 0
    deleting = False

    def finish(outcome: str, reason: str, message: str, *, attempted: bool = True) -> dict:
        result["pruned"] = (new_pruned + previous)[:_MAX_PRUNED_HISTORY]
        result["retention"] = retention_status(
            status,
            now,
            result=outcome,
            reason=reason,
            message=message,
            attempted=attempted,
            candidate=candidate_count,
            deleted=len(new_pruned),
            skipped=skipped_count,
        )
        logger.info("gc: %s: %s", reason, message)
        if outcome in {"Failed", "PartialFailure"}:
            emit_event(reason, message, type_="Warning")
        return result

    policy = spec.get("retention") or {}
    if not policy.get("enabled"):
        return finish("Skipped", "Disabled", "Retention is disabled", attempted=False)
    current = {
        e["flatcarVersion"] for e in status.get("observedNodes") or [] if e.get("flatcarVersion")
    }
    tracked = {
        e["flatcarVersion"]
        for e in status.get("trackedChannelVersions") or []
        if e.get("flatcarVersion")
    }
    pinned = set((spec.get("flatcar") or {}).get("versions") or [])
    if not current | tracked | pinned:
        return finish(
            "Skipped", "NoFlatcarVersions", "No required Flatcar versions", attempted=False
        )
    try:
        required = current | tracked | pinned
        if any(not re.fullmatch(r"\d+\.\d+\.\d+", v) for v in required):
            raise ValueError("Invalid required Flatcar version")
        min_age = parse_duration(policy.get("minAgeBeforeDelete", "168h"))
        keep = int(policy.get("keepPreviousFlatcarVersions", 0))
        if keep < 0:
            raise ValueError("keepPreviousFlatcarVersions must be nonnegative")
        repos = repositories(spec)
        if not repos:
            raise ValueError("No image repository configured")
        credentials = {
            repo: auth_by_repository[repo] if auth_by_repository is not None else auth
            for repo in repos
        }
        inventories = {
            repo: _registry.inventory_repository(repo, credentials[repo]) for repo in repos
        }
        history = {
            key.flatcar
            for inv in inventories.values()
            for tag in inv.tags
            if (key := parse_image_tag(tag)) is not None
        }
        retained = reconciler.compute_retained_flatcar_set(current, tracked | pinned, history, keep)
        result["retainedFlatcarVersions"] = sorted(retained)
        plans: dict[str, dict[str, list[str]]] = {}
        for repo, inv in inventories.items():
            eligible: set[str] = set()
            aliases: dict[str, list[str]] = {}
            for tag, digest in inv.tags.items():
                aliases.setdefault(digest, []).append(tag)
                key = parse_image_tag(tag)
                age = inv.manifests[digest].created_at
                if key and key.flatcar not in retained and age is not None:
                    if age.tzinfo is not None and age <= now and now - age >= min_age:
                        eligible.add(tag)
            candidate_count += len(eligible)
            deletable = {digest for digest, tags in aliases.items() if set(tags) <= eligible}
            # A preserved root protects its entire manifest/index reference closure.
            protected: set[str] = set()
            pending = list(set(aliases) - deletable)
            while pending:
                digest = pending.pop()
                if digest not in protected:
                    protected.add(digest)
                    pending.extend(inv.manifests[digest].children)
            deletable -= protected
            plans[repo] = {digest: aliases[digest] for digest in sorted(deletable)}
            skipped_count += len(inv.tags) - sum(len(tags) for tags in plans[repo].values())
        # Recheck ALL repositories before the first mutation, not halfway through deletion.
        if any(
            _registry.inventory_repository(repo, credentials[repo]) != inventories[repo]
            for repo in repos
        ):
            return finish("Skipped", "InventoryChanged", "Registry changed during retention")
        deleting = True
        for repo, plan in plans.items():
            # Delete parents before children so a partial failure never leaves a retained
            # parent pointing at a child deleted earlier in this attempt.
            pending = set(plan)
            while pending:
                referenced: set[str] = set()
                descendants = [
                    child
                    for digest in pending
                    for child in inventories[repo].manifests[digest].children
                ]
                while descendants:
                    child = descendants.pop()
                    if child not in referenced:
                        referenced.add(child)
                        descendants.extend(inventories[repo].manifests[child].children)
                roots = sorted(pending - referenced)
                if not roots:
                    raise _registry.RegistryError("Cyclic deletion plan")
                for digest in roots:
                    _registry.delete_manifest(repo, digest, credentials[repo])
                    for tag in sorted(plan[digest]):
                        new_pruned.append(
                            {
                                "tag": f"{repo}:{tag}",
                                "reason": "RetentionPolicy",
                                "prunedAt": now.isoformat(),
                            }
                        )
                    pending.remove(digest)
        return finish("Succeeded", "RetentionComplete", f"Deleted {len(new_pruned)} tag(s)")
    except (ValueError, KeyError, _registry.RegistryError) as exc:
        reason = "InventoryIncomplete"
        if isinstance(exc, _registry.RegistryUnreachable):
            reason = "RegistryUnreachable"
            result["_registryUnreachable"] = str(exc)
        elif isinstance(exc, TagDeletionDisabled):
            reason = "RegistryDeleteDisabled"
        elif isinstance(exc, (ValueError, KeyError)):
            reason = "InvalidConfiguration"
        elif deleting:
            reason = "RegistryDeleteFailed"
        return finish("PartialFailure" if new_pruned else "Failed", reason, str(exc))
