"""kopf operator handlers for VGPUDriverImage.

OUT OF SCOPE for unit tests in this phase:
- Integration / e2e tests (Phase G).
- Leader election (kopf handles it when run with --standalone=False).
- Webhook validation (future phase).

Handler registration happens at import time via kopf decorators; the module is
imported by ``cli.py`` before ``kopf.run()`` is called.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import kopf  # type: ignore[import-untyped]
from kubernetes import client, config  # type: ignore[import-untyped]

from vgpu_driver_operator import crd as _crd
from vgpu_driver_operator import flatcar as _flatcar
from vgpu_driver_operator import gc as _gc
from vgpu_driver_operator import job_factory as _jf
from vgpu_driver_operator import reconciler as _reconciler
from vgpu_driver_operator import registry as _registry

GROUP = "vgpu.flatcar.io"
VERSION = "v1alpha1"
PLURAL = "vgpudriverimages"

log = logging.getLogger(__name__)

# Module-level debounce state: node name → timestamp of last event.
_node_event_times: dict[str, float] = {}
_DEBOUNCE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.persistence.finalizer = "vgpu.flatcar.io/finalizer"
    settings.posting.level = logging.INFO


# ---------------------------------------------------------------------------
# Reconcile handler (create + update)
# ---------------------------------------------------------------------------


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
def reconcile(
    spec: dict,
    name: str,
    namespace: str,
    status: dict,
    patch: kopf.Patch,
    body: kopf.Body,
    logger: logging.Logger,
    **_: Any,
) -> None:
    """Main reconcile loop: compute desired builds, create missing Jobs, update status."""
    _do_reconcile(spec=spec, name=name, status=status, patch=patch, body=body, logger=logger)


# ---------------------------------------------------------------------------
# Node event handler (debounced)
# ---------------------------------------------------------------------------


@kopf.on.event("", "v1", "nodes")
def on_node_event(event: dict, logger: logging.Logger, **_: Any) -> None:
    """Debounced node-event handler.

    When a node changes, wait 5 s and, if no newer event has arrived for that
    node, trigger a reconcile of every VGPUDriverImage by touching them with
    an annotation.
    """
    obj = event.get("object") or {}
    node_name: str = (obj.get("metadata") or {}).get("name", "")
    if not node_name:
        return

    import time

    now = time.monotonic()
    _node_event_times[node_name] = now

    # Run the debounce in a background thread via asyncio (kopf event handlers
    # are synchronous; spawn a thread-based sleep).
    import threading

    def _debounced() -> None:
        import time as _time
        _time.sleep(_DEBOUNCE_SECONDS)
        if _node_event_times.get(node_name) != now:
            # A newer event arrived — let that one do the work.
            return
        _touch_all_vgpu_driver_images(logger)

    t = threading.Thread(target=_debounced, daemon=True)
    t.start()


def _touch_all_vgpu_driver_images(logger: logging.Logger) -> None:
    """Patch a touch-annotation on every VGPUDriverImage to trigger reconcile."""
    try:
        _load_k8s_config()
        custom_api = client.CustomObjectsApi()
        objects = _crd.list_vgpu_driver_images(custom_api)
    except Exception as exc:
        logger.warning("on_node_event: failed to list VGPUDriverImages: %s", exc)
        return

    ts = datetime.now(tz=timezone.utc).isoformat()
    for obj in objects:
        obj_name = (obj.get("metadata") or {}).get("name", "")
        if not obj_name:
            continue
        try:
            custom_api.patch_cluster_custom_object(
                GROUP,
                VERSION,
                PLURAL,
                obj_name,
                {
                    "metadata": {
                        "annotations": {
                            "vgpu.flatcar.io/node-touch": ts,
                        }
                    }
                },
            )
        except Exception as exc:
            logger.warning(
                "on_node_event: failed to touch %s: %s", obj_name, exc
            )


# ---------------------------------------------------------------------------
# Job event handler
# ---------------------------------------------------------------------------


@kopf.on.event("batch", "v1", "jobs", labels={"app": "vgpu-driver-builder"})
def on_job_event(event: dict, logger: logging.Logger, **_: Any) -> None:
    """Update CRD status.builds[] phase when a build Job changes."""
    obj = event.get("object") or {}
    meta = obj.get("metadata") or {}
    labels: dict = meta.get("labels") or {}

    owner_uid = labels.get("vgpu.flatcar.io/owner-uid")
    if not owner_uid:
        return

    job_status: dict = obj.get("status") or {}
    active = int(job_status.get("active", 0))
    succeeded = int(job_status.get("succeeded", 0))
    failed = int(job_status.get("failed", 0))

    if succeeded > 0:
        phase = "Ready"
    elif failed > 0:
        phase = "Failed"
    elif active > 0:
        phase = "Building"
    else:
        phase = "Pending"

    job_name = meta.get("name", "")
    driver_ver = labels.get("vgpu.flatcar.io/driver-version", "")
    flatcar_ver = labels.get("vgpu.flatcar.io/flatcar-version", "")

    if not driver_ver or not flatcar_ver:
        return

    # Find the owning CRD by UID and patch its status.
    try:
        _load_k8s_config()
        custom_api = client.CustomObjectsApi()
        objects = _crd.list_vgpu_driver_images(custom_api)
    except Exception as exc:
        logger.warning("on_job_event: cannot list VGPUDriverImages: %s", exc)
        return

    for crd_obj in objects:
        crd_meta = crd_obj.get("metadata") or {}
        if crd_meta.get("uid") != owner_uid:
            continue

        crd_name = crd_meta.get("name", "")
        crd_status: dict = crd_obj.get("status") or {}
        builds: list[dict] = list(crd_status.get("builds") or [])

        # Find and update the matching build entry.
        now_str = datetime.now(tz=timezone.utc).isoformat()
        updated = False
        for entry in builds:
            if (
                entry.get("driverVersion") == driver_ver
                and entry.get("flatcarVersion") == flatcar_ver
            ):
                old_phase = entry.get("phase")
                entry["phase"] = phase
                entry["jobName"] = job_name
                entry["lastTransitionTime"] = now_str
                updated = True
                if phase in ("Ready", "Failed") and phase != old_phase:
                    logger.info(
                        "job %s transitioned to %s (driver=%s flatcar=%s)",
                        job_name, phase, driver_ver, flatcar_ver,
                    )
                break

        if not updated:
            builds.append(
                {
                    "driverVersion": driver_ver,
                    "flatcarVersion": flatcar_ver,
                    "phase": phase,
                    "jobName": job_name,
                    "lastTransitionTime": now_str,
                }
            )

        try:
            _crd.patch_status(custom_api, crd_name, {"builds": builds})
        except Exception as exc:
            logger.warning(
                "on_job_event: failed to patch status for %s: %s", crd_name, exc
            )
        return


# ---------------------------------------------------------------------------
# Periodic timer (safety-net reconcile every 10 min)
# ---------------------------------------------------------------------------


@kopf.timer(GROUP, VERSION, PLURAL, interval=600.0, idle=300.0)
def periodic(
    spec: dict,
    name: str,
    status: dict,
    patch: kopf.Patch,
    body: kopf.Body,
    logger: logging.Logger,
    **_: Any,
) -> None:
    """10-minute safety-net reconcile."""
    _do_reconcile(spec=spec, name=name, status=status, patch=patch, body=body, logger=logger)


# ---------------------------------------------------------------------------
# Shared reconcile logic
# ---------------------------------------------------------------------------


def _do_reconcile(
    *,
    spec: dict,
    name: str,
    status: dict,
    patch: kopf.Patch,
    body: kopf.Body,
    logger: logging.Logger,
) -> None:
    """Common reconcile logic shared by reconcile() and periodic()."""
    _load_k8s_config()
    now = datetime.now(tz=timezone.utc)
    now_str = now.isoformat()
    op_ns = _crd.operator_namespace()

    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()
    custom_api = client.CustomObjectsApi()

    # --- 1. Collect node pairs ---
    flatcar_cfg: dict = spec.get("flatcar") or {}
    node_selector: dict = flatcar_cfg.get("nodeSelector") or {}

    label_selector = ",".join(f"{k}={v}" for k, v in node_selector.items()) or None
    try:
        nodes_resp = core_api.list_node(label_selector=label_selector)
        nodes = [n.to_dict() for n in nodes_resp.items]
    except Exception as exc:
        logger.warning("reconcile: failed to list nodes: %s", exc)
        nodes = []

    node_pairs: set[tuple[str, str]] = set()
    observed_nodes: list[dict] = []
    for node in nodes:
        fv = _flatcar.flatcar_version_from_node(node)
        kv = _flatcar.kernel_version_from_node(node)
        if fv and kv:
            node_pairs.add((fv, kv))

    # Build observedNodes (deduplicated by (flatcar, kernel), with nodeCount).
    pair_counts: dict[tuple[str, str], int] = {}
    for node in nodes:
        fv = _flatcar.flatcar_version_from_node(node)
        kv = _flatcar.kernel_version_from_node(node)
        if fv and kv:
            pair_counts[(fv, kv)] = pair_counts.get((fv, kv), 0) + 1
    for (fv, kv), count in pair_counts.items():
        observed_nodes.append(
            {"flatcarVersion": fv, "kernelVersion": kv, "nodeCount": count}
        )

    # --- 2. Tracked pairs from poller ---
    tracked_entries: list[dict] = (status or {}).get("trackedChannelVersions") or []
    tracked_pairs: set[tuple[str, str]] = {
        (e["flatcarVersion"], e["kernelVersion"])
        for e in tracked_entries
        if e.get("flatcarVersion") and e.get("kernelVersion")
    }

    # --- 3. Compute desired ---
    driver_versions: list[str] = spec.get("driverVersions") or []
    precompile: bool = bool(spec.get("precompile", False))
    desired = _reconciler.compute_desired(
        driver_versions, node_pairs, tracked_pairs, precompile=precompile
    )

    # --- 4. Auth + existing tags ---
    registry_cfg: dict = spec.get("registry") or {}
    repo_runtime: str = registry_cfg.get("repository", "")
    repo_precompile: str = registry_cfg.get("repositoryPrecompiled", "")

    auth_secret_name: str = (registry_cfg.get("authSecretRef") or {}).get("name", "")
    reg_auth: _registry.RegistryAuth | None = None
    if auth_secret_name:
        try:
            secret_data = _crd.get_secret(core_api, op_ns, auth_secret_name)
            dockercfg = secret_data.get(".dockerconfigjson") or secret_data.get(
                "config.json", b""
            )
            host = repo_runtime.split("/")[0] if repo_runtime else ""
            if dockercfg and host:
                reg_auth = _registry.parse_dockerconfigjson(dockercfg, host)
        except Exception as exc:
            logger.warning("reconcile: failed to load registry auth: %s", exc)

    active_repo = repo_precompile if (precompile and repo_precompile) else repo_runtime
    existing_tags: set[str] = set()
    if active_repo:
        try:
            existing_tags = _registry.list_tags(active_repo, reg_auth)
        except _registry.RegistryError as exc:
            logger.warning("reconcile: failed to list tags from %s: %s", active_repo, exc)

    # --- 5. In-flight Jobs ---
    crd_uid = (body.get("metadata") or {}).get("uid", "")
    try:
        jobs = _crd.list_owned_jobs(batch_api, op_ns, crd_uid)
    except Exception as exc:
        logger.warning("reconcile: failed to list owned jobs: %s", exc)
        jobs = []

    in_flight: set[_reconciler.BuildKey] = set()
    job_phase_map: dict[str, str] = {}  # job_name -> phase
    job_key_map: dict[_reconciler.BuildKey, str] = {}  # key -> job_name
    for job in jobs:
        jmeta = job.get("metadata") or {}
        jlabels: dict = jmeta.get("labels") or {}
        jstatus: dict = job.get("status") or {}
        jname = jmeta.get("name", "")

        dv = jlabels.get("vgpu.flatcar.io/driver-version", "")
        fv = jlabels.get("vgpu.flatcar.io/flatcar-version", "")
        if not (dv and fv):
            continue

        # Determine phase from job status.
        active = int(jstatus.get("active", 0))
        succeeded = int(jstatus.get("succeeded", 0))
        failed = int(jstatus.get("failed", 0))
        if succeeded > 0:
            phase = "Ready"
        elif failed > 0:
            phase = "Failed"
        elif active > 0:
            phase = "Building"
        else:
            phase = "Pending"

        if precompile:
            key = _reconciler.parse_precompile_tag(
                jlabels.get("vgpu.flatcar.io/image-tag", "")
            )
        else:
            key = _reconciler.parse_runtime_tag(
                jlabels.get("vgpu.flatcar.io/image-tag", "")
            )
        # Fallback: build a key from labels (no kernel for runtime).
        if key is None:
            key = _reconciler.BuildKey(driver=dv, flatcar=fv, kernel=None)

        if phase in ("Pending", "Building"):
            in_flight.add(key)
        job_phase_map[jname] = phase
        job_key_map[key] = jname

    # --- 6. Compute missing ---
    missing = _reconciler.compute_missing(desired, existing_tags, in_flight, precompile=precompile)

    # --- 7. Create Jobs for missing keys ---
    s3_secret_name: str = (
        (spec.get("source") or {}).get("credentialsSecretRef") or {}
    ).get("name") or os.environ.get("S3_SECRET_NAME", "s3-driver-storage-secret")
    reg_secret_name: str | None = auth_secret_name or os.environ.get(
        "REGISTRY_SECRET_NAME", "private-registry-secret"
    ) or None
    dockerfile_cm = os.environ.get("DOCKERFILE_CONFIGMAP", "driver-build-files")
    buildfiles_cm = os.environ.get("BUILDFILES_CONFIGMAP", "driver-build-files")
    crd_name = name

    for key in missing:
        manifest = _jf.build_job_manifest(
            crd_namespace=op_ns,
            crd_name=crd_name,
            crd_uid=crd_uid,
            spec=spec,
            key=key,
            s3_secret_name=s3_secret_name,
            registry_secret_name=reg_secret_name,
            dockerfile_configmap=dockerfile_cm,
            buildfiles_configmap=buildfiles_cm,
            build_created=now_str,
        )
        jname = manifest["metadata"]["name"]
        try:
            batch_api.create_namespaced_job(op_ns, manifest)
            logger.info("reconcile: created Job %s", jname)
        except client.ApiException as exc:
            if exc.status == 409:
                logger.debug("reconcile: Job %s already exists, skipping", jname)
            else:
                logger.warning("reconcile: failed to create Job %s: %s", jname, exc)

    # --- 8. Build status payload ---
    tag_fn = _reconciler.precompile_tag if precompile else _reconciler.runtime_tag
    parse_fn = _reconciler.parse_precompile_tag if precompile else _reconciler.parse_runtime_tag

    # Map existing tags → BuildKey for phase derivation.
    built_keys: set[_reconciler.BuildKey] = set()
    for tag in existing_tags:
        k = parse_fn(tag)
        if k is not None:
            built_keys.add(k)

    builds: list[dict] = []
    for key in sorted(desired, key=lambda k: (k.driver, k.flatcar, k.kernel or "")):
        tag = tag_fn(key)
        jname = job_key_map.get(key, "")
        if key in built_keys:
            phase = "Ready"
        elif key in in_flight:
            phase = job_phase_map.get(jname, "Building")
        else:
            phase = "Pending"

        mode = "precompiled" if precompile else "compiled"
        entry: dict = {
            "driverVersion": key.driver,
            "flatcarVersion": key.flatcar,
            "mode": mode,
            "tag": tag,
            "phase": phase,
            "lastTransitionTime": now_str,
        }
        if key.kernel:
            entry["kernelVersion"] = key.kernel
        if jname:
            entry["jobName"] = jname
        builds.append(entry)

    # Reconciled condition.
    all_ready = all(b["phase"] == "Ready" for b in builds) if builds else True
    condition = {
        "type": "Reconciled",
        "status": "True" if all_ready else "False",
        "reason": "AllBuildsReady" if all_ready else "BuildsPending",
        "message": (
            f"{len(builds)} build(s) ready"
            if all_ready
            else f"{sum(1 for b in builds if b['phase'] != 'Ready')} build(s) pending"
        ),
        "lastTransitionTime": now_str,
    }

    new_status: dict = {
        "observedNodes": observed_nodes,
        "builds": builds,
        "conditions": [condition],
    }

    # --- 9. GC (if enabled) ---
    retention: dict = spec.get("retention") or {}
    if retention.get("enabled") and reg_auth is not None:
        # Merge observedNodes into status before passing to gc.run so it can
        # use the freshly-computed set.
        merged_status = dict(status or {})
        merged_status["observedNodes"] = observed_nodes

        def _emit(reason: str, message: str, type_: str = "Normal") -> None:
            try:
                kopf.event(body, type=type_, reason=reason, message=message)
            except Exception:
                pass

        try:
            gc_result = _gc.run(
                spec=spec,
                status=merged_status,
                auth=reg_auth,
                now=now,
                logger=logger,
                emit_event=_emit,
            )
            new_status.update(gc_result)
        except Exception as exc:
            logger.warning("reconcile: GC failed: %s", exc)

    # --- 10. Apply status patch ---
    patch.status.update(new_status)

    # --- 11. Emit summary event ---
    summary = (
        f"Reconciled: {len(builds)} build(s), "
        f"{sum(1 for b in builds if b['phase'] == 'Ready')} ready, "
        f"{len(missing)} job(s) created"
    )
    logger.info("reconcile: %s", summary)
    try:
        kopf.event(body, type="Normal", reason="Reconciled", message=summary)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Kubernetes config loader
# ---------------------------------------------------------------------------


def _load_k8s_config() -> None:
    """Load in-cluster config or fall back to kubeconfig."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        try:
            config.load_kube_config()
        except config.ConfigException:
            pass  # No config available — tests inject mock clients directly.
