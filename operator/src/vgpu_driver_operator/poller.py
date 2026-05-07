"""Flatcar release-feed poller.

Run as a Kubernetes CronJob to keep ``status.trackedChannelVersions`` up to
date on all VGPUDriverImage objects.  The CronJob marks failed if any channel
fetch fails (non-zero exit), but other channels are still processed.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

import requests

from vgpu_driver_operator import flatcar as _flatcar
from vgpu_driver_operator import crd as _crd
from kubernetes import client, config  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_once(
    *,
    custom_api: client.CustomObjectsApi | None = None,
    session: requests.Session | None = None,
) -> int:
    """Poll every tracked channel for every VGPUDriverImage and patch status.

    Parameters
    ----------
    custom_api:
        Injected CustomObjectsApi for testing; when ``None`` the in-cluster or
        kubeconfig client is used.
    session:
        Injected ``requests.Session`` for HTTP calls; when ``None`` a fresh
        session is created.

    Returns
    -------
    0
        All channels polled successfully.
    1
        At least one channel fetch failed (other channels still updated).
    """
    _configure_k8s()
    api = custom_api or client.CustomObjectsApi()
    sess = session or requests.Session()

    try:
        objects = _crd.list_vgpu_driver_images(api)
    except Exception as exc:
        log.error("poller: failed to list VGPUDriverImages: %s", exc)
        return 1

    any_failure = False

    for obj in objects:
        name: str = (obj.get("metadata") or {}).get("name", "<unknown>")
        spec: dict = obj.get("spec") or {}
        flatcar_cfg: dict = spec.get("flatcar") or {}
        channels: list[str] = flatcar_cfg.get("trackChannels") or []
        arch: str = flatcar_cfg.get("arch", "amd64")

        if not channels:
            log.debug("poller: %s has no trackChannels, skipping", name)
            continue

        log.info("poller: processing %s channels=%s", name, channels)

        updated_entries: list[dict] = []
        for channel in channels:
            try:
                version = _flatcar.latest_release(channel, arch, session=sess)
            except _flatcar.FlatcarFeedError as exc:
                log.error(
                    "poller: %s channel=%s fetch failed: %s", name, channel, exc
                )
                any_failure = True
                continue

            log.info(
                "poller: %s channel=%s flatcar=%s",
                name, channel, version,
            )
            updated_entries.append(
                {
                    "channel": channel,
                    "flatcarVersion": version,
                    "observedAt": datetime.now(tz=timezone.utc).isoformat(),
                }
            )

        if not updated_entries:
            continue

        # Merge with existing entries (keep channels we didn't re-fetch).
        existing: list[dict] = (obj.get("status") or {}).get(
            "trackedChannelVersions", []
        ) or []
        merged = _merge_channel_entries(existing, updated_entries)

        try:
            _crd.patch_status(api, name, {"trackedChannelVersions": merged})
            log.info("poller: patched status for %s (%d channels)", name, len(merged))
        except Exception as exc:
            log.error("poller: failed to patch status for %s: %s", name, exc)
            any_failure = True

    return 1 if any_failure else 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _merge_channel_entries(
    existing: list[dict],
    updated: list[dict],
) -> list[dict]:
    """Merge updated channel entries into existing list.

    Entries for channels in ``updated`` replace the corresponding entry in
    ``existing``; entries for channels not in ``updated`` are kept as-is.
    """
    by_channel = {e["channel"]: e for e in existing}
    for entry in updated:
        by_channel[entry["channel"]] = entry
    return list(by_channel.values())


def _configure_k8s() -> None:
    """Try in-cluster config, fall back to kubeconfig (for local dev)."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        try:
            config.load_kube_config()
        except config.ConfigException:
            pass  # Tests inject a mock API directly
