"""Tests for stale Flatcar poller Job archival."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

from vgpu_driver_operator import stale_poller_archive as _archive


def _cfg() -> _archive.ArchiveConfig:
    return _archive.ArchiveConfig(
        namespace="vgpu-driver-operator",
        release_name="vgpu",
        poller_job_prefix="vgpu-vgpu-driver-operator-flatcar-poll-",
        new_image="ghcr.io/example/vgpu-driver-operator:new",
        chart_version="2026.6.10",
        app_version="v2026.06.10",
    )


def _job(
    name: str = "vgpu-vgpu-driver-operator-flatcar-poll-123",
    *,
    component: str | None = "flatcar-poller",
    failed: int = 1,
    succeeded: int = 0,
    complete: bool = False,
    image: str | None = "ghcr.io/example/vgpu-driver-operator:old",
):
    labels = {}
    if component is not None:
        labels["app.kubernetes.io/component"] = component
    conditions = []
    if complete:
        conditions.append(SimpleNamespace(type="Complete", status="True"))
    containers = [SimpleNamespace(name="flatcar-poller", image=image)]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels),
        status=SimpleNamespace(
            failed=failed,
            succeeded=succeeded,
            conditions=conditions,
        ),
        spec=SimpleNamespace(
            template=SimpleNamespace(
                spec=SimpleNamespace(containers=containers),
            ),
        ),
    )


def _api_with_jobs(*jobs):
    api = MagicMock()
    api.list_namespaced_job.return_value = SimpleNamespace(items=list(jobs))
    return api


def test_archives_stale_old_image(monkeypatch):
    monkeypatch.setattr(_archive, "_utc_now", lambda: "2026-06-03T01:02:03Z")
    api = _api_with_jobs(_job(failed=3))

    result = _archive.archive_stale_failed_poller_jobs(api, _cfg())

    assert result.archived == 1
    assert result.skipped == 0
    metadata_patch = api.patch_namespaced_job.call_args_list[1].args[2]
    assert metadata_patch["metadata"]["labels"] == {
        "vgpu.flatcar.io/archived-stale-poller": "true",
        "vgpu.flatcar.io/archive-reason": "stale-failed-poller",
    }
    assert metadata_patch["metadata"]["annotations"] == {
        "vgpu.flatcar.io/archive-previous-image": (
            "ghcr.io/example/vgpu-driver-operator:old"
        ),
        "vgpu.flatcar.io/archive-previous-failed-count": "3",
        "vgpu.flatcar.io/archive-timestamp": "2026-06-03T01:02:03Z",
        "vgpu.flatcar.io/archive-chart-version": "2026.6.10",
        "vgpu.flatcar.io/archive-app-version": "v2026.06.10",
    }


def test_skips_current_image():
    api = _api_with_jobs(_job(image="ghcr.io/example/vgpu-driver-operator:new"))

    result = _archive.archive_stale_failed_poller_jobs(api, _cfg())

    assert result.archived == 0
    assert result.skipped == 1
    api.patch_namespaced_job.assert_not_called()
    api.patch_namespaced_job_status.assert_not_called()


def test_skips_successful_job():
    api = _api_with_jobs(_job(succeeded=1), _job(complete=True))

    result = _archive.archive_stale_failed_poller_jobs(api, _cfg())

    assert result.archived == 0
    assert result.skipped == 2
    api.patch_namespaced_job.assert_not_called()


def test_legacy_name_prefix_matches_without_component_label():
    api = _api_with_jobs(
        _job(
            name="vgpu-vgpu-driver-operator-flatcar-poll-legacy",
            component=None,
        )
    )

    result = _archive.archive_stale_failed_poller_jobs(api, _cfg())

    assert result.archived == 1
    api.patch_namespaced_job.assert_called()


def test_component_label_matches_without_legacy_name_prefix():
    api = _api_with_jobs(
        _job(
            name="new-cronjob-name-123",
            component="flatcar-poller",
        )
    )

    result = _archive.archive_stale_failed_poller_jobs(api, _cfg())

    assert result.archived == 1
    api.patch_namespaced_job.assert_called()


def test_patch_order_suspends_before_metadata_and_status(monkeypatch):
    monkeypatch.setattr(_archive, "_utc_now", lambda: "2026-06-03T01:02:03Z")
    api = _api_with_jobs(_job())

    _archive.archive_stale_failed_poller_jobs(api, _cfg())

    assert api.method_calls == [
        call.list_namespaced_job(
            "vgpu-driver-operator",
            label_selector="app.kubernetes.io/instance=vgpu",
        ),
        call.patch_namespaced_job(
            "vgpu-vgpu-driver-operator-flatcar-poll-123",
            "vgpu-driver-operator",
            {"spec": {"suspend": True}},
            _content_type=_archive.MERGE_PATCH_CONTENT_TYPE,
        ),
        call.patch_namespaced_job(
            "vgpu-vgpu-driver-operator-flatcar-poll-123",
            "vgpu-driver-operator",
            _archive._metadata_patch(
                "ghcr.io/example/vgpu-driver-operator:old",
                1,
                "2026-06-03T01:02:03Z",
                _cfg(),
            ),
            _content_type=_archive.MERGE_PATCH_CONTENT_TYPE,
        ),
        call.patch_namespaced_job_status(
            "vgpu-vgpu-driver-operator-flatcar-poll-123",
            "vgpu-driver-operator",
            {"status": {"failed": 0, "conditions": []}},
            _content_type=_archive.MERGE_PATCH_CONTENT_TYPE,
        ),
    ]


def test_all_patches_use_explicit_merge_patch_content_type():
    api = _api_with_jobs(_job())

    _archive.archive_stale_failed_poller_jobs(api, _cfg())

    for patch_call in api.patch_namespaced_job.call_args_list:
        assert patch_call.kwargs["_content_type"] == _archive.MERGE_PATCH_CONTENT_TYPE
    assert (
        api.patch_namespaced_job_status.call_args.kwargs["_content_type"]
        == _archive.MERGE_PATCH_CONTENT_TYPE
    )
