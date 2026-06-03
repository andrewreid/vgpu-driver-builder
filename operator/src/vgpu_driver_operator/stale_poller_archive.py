"""Archive stale failed Flatcar poller Jobs during Helm upgrades."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from kubernetes import client, config  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

MERGE_PATCH_CONTENT_TYPE = "application/merge-patch+json"


@dataclass(frozen=True)
class ArchiveConfig:
    """Runtime configuration for stale poller Job archival."""

    namespace: str
    release_name: str
    poller_job_prefix: str
    new_image: str
    chart_version: str
    app_version: str


@dataclass(frozen=True)
class ArchiveResult:
    """Summary of one stale poller archive pass."""

    archived: int
    skipped: int
    selector: str
    poller_job_prefix: str


def run_from_env(
    *,
    batch_api: client.BatchV1Api | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Load hook configuration from environment variables and run archival."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = config_from_env(os.environ if environ is None else environ)

    if batch_api is None:
        config.load_incluster_config()
        batch_api = client.BatchV1Api()

    archive_stale_failed_poller_jobs(batch_api, cfg)
    return 0


def config_from_env(environ: Mapping[str, str]) -> ArchiveConfig:
    """Build archive configuration from the Helm hook environment."""
    return ArchiveConfig(
        namespace=environ["POD_NAMESPACE"],
        release_name=environ["RELEASE_NAME"],
        poller_job_prefix=environ["POLLER_JOB_NAME_PREFIX"],
        new_image=environ["NEW_POLLER_IMAGE"],
        chart_version=environ["CLEANUP_CHART_VERSION"],
        app_version=environ["CLEANUP_APP_VERSION"],
    )


def archive_stale_failed_poller_jobs(
    batch_api: client.BatchV1Api,
    cfg: ArchiveConfig,
) -> ArchiveResult:
    """Archive failed poller Jobs that belong to an older operator image."""
    selector = f"app.kubernetes.io/instance={cfg.release_name}"
    jobs = batch_api.list_namespaced_job(
        cfg.namespace,
        label_selector=selector,
    ).items
    archived = 0
    skipped = 0

    for job in jobs:
        candidate = _archive_candidate(job, cfg)
        if candidate is None:
            skipped += 1
            continue

        name, previous_image, failed = candidate
        archived_at = _utc_now()

        batch_api.patch_namespaced_job(
            name,
            cfg.namespace,
            {"spec": {"suspend": True}},
            _content_type=MERGE_PATCH_CONTENT_TYPE,
        )
        batch_api.patch_namespaced_job(
            name,
            cfg.namespace,
            _metadata_patch(previous_image, failed, archived_at, cfg),
            _content_type=MERGE_PATCH_CONTENT_TYPE,
        )
        batch_api.patch_namespaced_job_status(
            name,
            cfg.namespace,
            {"status": {"failed": 0, "conditions": []}},
            _content_type=MERGE_PATCH_CONTENT_TYPE,
        )

        log.info(
            "archived stale failed Flatcar poller Job %s: %s -> %s",
            name,
            previous_image,
            cfg.new_image,
        )
        archived += 1

    result = ArchiveResult(
        archived=archived,
        skipped=skipped,
        selector=selector,
        poller_job_prefix=cfg.poller_job_prefix,
    )
    log.info(
        "archive complete: archived=%d skipped=%d selector=%s poller_job_prefix=%s",
        result.archived,
        result.skipped,
        result.selector,
        result.poller_job_prefix,
    )
    return result


def _archive_candidate(job: Any, cfg: ArchiveConfig) -> tuple[str, str, int] | None:
    name = _value(job, "metadata.name")
    labels = _value(job, "metadata.labels") or {}
    component = labels.get("app.kubernetes.io/component")
    if component != "flatcar-poller" and not str(name).startswith(cfg.poller_job_prefix):
        return None

    if _is_complete(job):
        return None

    failed = int(_value(job, "status.failed") or 0)
    if failed <= 0:
        return None

    previous_image = _poller_image(job)
    if not previous_image or previous_image == cfg.new_image:
        return None

    return str(name), previous_image, failed


def _is_complete(job: Any) -> bool:
    if int(_value(job, "status.succeeded") or 0) > 0:
        return True

    conditions = _value(job, "status.conditions") or []
    return any(
        _value(condition, "type") == "Complete"
        and _value(condition, "status") == "True"
        for condition in conditions
    )


def _poller_image(job: Any) -> str | None:
    containers = _value(job, "spec.template.spec.containers") or []
    for container in containers:
        if _value(container, "name") == "flatcar-poller":
            image = _value(container, "image")
            return str(image) if image else None
    return None


def _metadata_patch(
    previous_image: str,
    failed: int,
    archived_at: str,
    cfg: ArchiveConfig,
) -> dict[str, dict[str, dict[str, str]]]:
    return {
        "metadata": {
            "labels": {
                "vgpu.flatcar.io/archived-stale-poller": "true",
                "vgpu.flatcar.io/archive-reason": "stale-failed-poller",
            },
            "annotations": {
                "vgpu.flatcar.io/archive-previous-image": previous_image,
                "vgpu.flatcar.io/archive-previous-failed-count": str(failed),
                "vgpu.flatcar.io/archive-timestamp": archived_at,
                "vgpu.flatcar.io/archive-chart-version": cfg.chart_version,
                "vgpu.flatcar.io/archive-app-version": cfg.app_version,
            },
        }
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _value(obj: Any, path: str) -> Any:
    current = obj
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


if __name__ == "__main__":
    raise SystemExit(run_from_env())
