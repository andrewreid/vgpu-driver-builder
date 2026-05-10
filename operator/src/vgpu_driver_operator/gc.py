"""Garbage collection of stale vGPU driver images.

Determines which registry tags are safe to delete based on the retention
policy in the CRD spec, then deletes them via the registry API.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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


def run(
    spec: dict,
    status: dict,
    *,
    auth: RegistryAuth,
    now: datetime,
    logger,
    emit_event: Callable[..., None],
) -> dict:
    """Run garbage collection and return a status fragment to merge.

    Returns a dict suitable for merging into the CRD status::

        {
            "pruned": [...],
            "retainedFlatcarVersions": [...],
        }

    Parameters
    ----------
    spec:
        The CRD ``.spec`` dict.
    status:
        The CRD ``.status`` dict (may be empty for a brand-new object).
    auth:
        Registry credentials.
    now:
        Current UTC timestamp (injected for determinism in tests).
    logger:
        Logger (kopf or stdlib).
    emit_event:
        Callable accepting ``(reason, message, type_="Normal")`` — used to
        publish Kubernetes events.
    """
    retention: dict = spec.get("retention") or {}
    keep_previous: int = int(retention.get("keepPreviousFlatcarVersions", 0))
    min_age_str: str = retention.get("minAgeBeforeDelete", "168h")
    min_age: timedelta = parse_duration(min_age_str)

    precompile: bool = bool(spec.get("precompile", False))
    registry_cfg: dict = spec.get("registry") or {}
    repo_runtime: str = registry_cfg.get("repository", "")
    repo_precompile: str = registry_cfg.get("repositoryPrecompiled", "")

    # --- Collect Flatcar version sets from status ---
    current_flatcars: set[str] = {
        entry["flatcarVersion"]
        for entry in (status.get("observedNodes") or [])
        if entry.get("flatcarVersion")
    }
    tracked_flatcars: set[str] = {
        entry["flatcarVersion"]
        for entry in (status.get("trackedChannelVersions") or [])
        if entry.get("flatcarVersion")
    }

    # History from previously-pruned entries.
    prev_pruned: list[dict] = list(status.get("pruned") or [])
    history_flatcars: set[str] = {
        entry["tag"].split("-flatcar")[-1]
        for entry in prev_pruned
        if "-flatcar" in entry.get("tag", "")
    }

    # Also add Flatcar versions found in registry tags.
    repos_to_check = [r for r in [repo_runtime, repo_precompile] if r]
    all_registry_tags: dict[str, set[str]] = {}
    registry_unreachable: _registry.RegistryUnreachable | None = None
    for repo in repos_to_check:
        try:
            tags = _registry.list_tags(repo, auth)
        except _registry.RegistryUnreachable as exc:
            logger.warning("gc: registry unreachable for %s: %s", repo, exc)
            registry_unreachable = registry_unreachable or exc
            tags = set()
        except _registry.RegistryError as exc:
            logger.warning("gc: list_tags failed for %s: %s", repo, exc)
            tags = set()
        all_registry_tags[repo] = tags
        parse_fn = reconciler.parse_precompile_tag if precompile else reconciler.parse_runtime_tag
        for tag in tags:
            key = parse_fn(tag)
            if key is not None:
                history_flatcars.add(key.flatcar)

    if registry_unreachable is not None:
        retained = reconciler.compute_retained_flatcar_set(
            current_flatcars,
            tracked_flatcars,
            history_flatcars,
            keep_previous,
        )
        return {
            "pruned": prev_pruned[:_MAX_PRUNED_HISTORY],
            "retainedFlatcarVersions": sorted(retained),
            "_registryUnreachable": str(registry_unreachable),
        }

    retained: set[str] = reconciler.compute_retained_flatcar_set(
        current_flatcars,
        tracked_flatcars,
        history_flatcars,
        keep_previous,
    )

    # --- Per-repo pruning ---
    new_pruned: list[dict] = []
    # Track which repos have already emitted a delete-disabled warning.
    delete_disabled_repos: set[str] = set()

    repos_config = [(repo_runtime, False)]
    if precompile and repo_precompile:
        repos_config.append((repo_precompile, True))

    for repo, is_precompile in repos_config:
        if not repo:
            continue
        tags = all_registry_tags.get(repo, set())
        if not tags:
            continue

        # Fetch tag ages.
        tag_ages: dict[str, datetime | None] = {}
        for tag in tags:
            try:
                tag_ages[tag] = _registry.tag_created_at(repo, tag, auth)
            except _registry.RegistryUnreachable as exc:
                logger.warning(
                    "gc: registry unreachable while reading %s:%s: %s",
                    repo,
                    tag,
                    exc,
                )
                registry_unreachable = registry_unreachable or exc
                tag_ages[tag] = None

        prunable = reconciler.compute_prunable(
            tags,
            tag_ages,
            retained,
            min_age,
            now=now,
            precompile=is_precompile,
        )

        for tag in sorted(prunable):  # sorted for determinism
            if repo in delete_disabled_repos:
                break
            try:
                _registry.delete_tag(repo, tag, auth)
                logger.info("gc: deleted %s:%s", repo, tag)
                new_pruned.append(
                    {
                        "tag": f"{repo}:{tag}",
                        "reason": "RetentionPolicy",
                        "prunedAt": now.isoformat(),
                    }
                )
            except TagDeletionDisabled as exc:
                logger.warning("gc: tag deletion disabled for %s: %s", repo, exc)
                emit_event(
                    "RegistryDeleteDisabled",
                    f"Registry does not support tag deletion for {repo}: {exc}",
                    type_="Warning",
                )
                delete_disabled_repos.add(repo)
                break
            except _registry.RegistryUnreachable as exc:
                logger.warning(
                    "gc: registry unreachable while deleting %s:%s: %s",
                    repo,
                    tag,
                    exc,
                )
                registry_unreachable = registry_unreachable or exc
                break
            except _registry.RegistryError as exc:
                logger.warning("gc: delete_tag failed for %s:%s: %s", repo, tag, exc)

    # Merge new pruned entries with previous history, capped at max.
    merged_pruned = (new_pruned + prev_pruned)[:_MAX_PRUNED_HISTORY]

    result = {
        "pruned": merged_pruned,
        "retainedFlatcarVersions": sorted(retained),
    }
    if registry_unreachable is not None:
        result["_registryUnreachable"] = str(registry_unreachable)
    return result
