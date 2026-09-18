"""Tests for gc.py — parse_duration and gc.run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from vgpu_driver_operator.gc import parse_duration, run as gc_run
from vgpu_driver_operator import gc
from vgpu_driver_operator.registry import (
    ManifestInfo,
    RepositoryInventory,
    RegistryError,
    RegistryUnreachable,
    TagDeletionDisabled,
)

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_hours(self):
        assert parse_duration("168h") == timedelta(hours=168)

    def test_days(self):
        assert parse_duration("7d") == timedelta(days=7)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_seconds_explicit(self):
        assert parse_duration("3600s") == timedelta(seconds=3600)

    def test_no_unit_means_seconds(self):
        assert parse_duration("60") == timedelta(seconds=60)

    def test_float_hours(self):
        assert parse_duration("1.5h") == timedelta(hours=1.5)

    def test_whitespace_stripped(self):
        assert parse_duration("  24h  ") == timedelta(hours=24)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_duration("garbage")

    def test_invalid_unit_raises(self):
        with pytest.raises(ValueError):
            parse_duration("10x")


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
REPO = "registry.example.com/drivers"
SPEC = {
    "registry": {"repository": REPO},
    "retention": {
        "enabled": True,
        "keepPreviousFlatcarVersions": 1,
        "minAgeBeforeDelete": "168h",
    },
}
STATUS = {"observedNodes": [{"flatcarVersion": "4757.2.0"}]}


def tag(version, precompiled=False):
    return f"550.54.15-{'6.12.1-flatcar-' if precompiled else ''}flatcar{version}"


def digest(n):
    return "sha256:" + f"{n:064x}"


def inventory(entries):
    tags, manifests = {}, {}
    for i, entry in enumerate(entries, 1):
        name, age, *extra = entry
        d = extra[0] if extra else digest(i)
        tags[name] = d
        manifests[d] = ManifestInfo(d, (), age)
    return RepositoryInventory(tags, manifests)


def setup_gc(monkeypatch, inv):
    read = MagicMock(return_value=inv)
    delete = MagicMock()
    monkeypatch.setattr(gc._registry, "inventory_repository", read)
    monkeypatch.setattr(gc._registry, "delete_manifest", delete)
    return read, delete


def run(spec=None, status=None, **kwargs):
    return gc_run(
        spec or SPEC,
        STATUS if status is None else status,
        auth=None,
        now=NOW,
        logger=MagicMock(),
        emit_event=MagicMock(),
        **kwargs,
    )


def test_anonymous_retains_current_tracked_pinned_and_previous(monkeypatch):
    inv = inventory(
        [(tag(v), OLD) for v in ["4757.2.0", "4593.2.3", "4593.2.2", "4230.2.0", "5000.1.0"]]
    )
    read, delete = setup_gc(monkeypatch, inv)
    spec = {**SPEC, "flatcar": {"versions": ["4230.2.0"]}}
    status = {**STATUS, "trackedChannelVersions": [{"flatcarVersion": "5000.1.0"}]}
    result = run(spec, status)
    delete.assert_called_once_with(REPO, digest(3), None)
    assert read.call_count == 2
    assert set(result["retainedFlatcarVersions"]) == {
        "4757.2.0",
        "4593.2.3",
        "4230.2.0",
        "5000.1.0",
    }
    assert result["retention"]["result"] == "Succeeded"
    assert result["retention"]["deletedCount"] == 1
    assert result["retention"]["lastSuccessTime"] == NOW.isoformat()


@pytest.mark.parametrize("precompiled", [False, True])
def test_shared_repository_and_deleted_history_does_not_consume_rollback(monkeypatch, precompiled):
    inv = inventory([(tag("4593.2.3", precompiled), OLD), (tag("4230.2.0", True), OLD)])
    read, delete = setup_gc(monkeypatch, inv)
    result = run(
        {**SPEC, "precompile": True},
        {
            **STATUS,
            "pruned": [
                {"tag": REPO + ":" + tag("4700.2.0"), "reason": "RetentionPolicy"},
            ],
        },
    )
    assert read.call_count == 2
    delete.assert_called_once_with(REPO, digest(2), None)
    assert "4593.2.3" in result["retainedFlatcarVersions"]


def test_separate_repositories_use_per_host_credentials(monkeypatch):
    other = "other.example.com/precompiled"
    inv = inventory([(tag("4000.2.0"), OLD)])
    read, delete = setup_gc(monkeypatch, inv)
    spec = {
        **SPEC,
        "precompile": True,
        "registry": {
            "repository": REPO,
            "repositoryPrecompiled": other,
            "cacheRepository": "cache.example.com/cache",
        },
        "retention": {"enabled": True},
    }
    auth = {REPO: None, other: {"username": "u", "password": "p"}}
    run(spec, auth_by_repository=auth)
    assert read.call_count == 4
    assert delete.call_count == 2
    delete.assert_any_call(other, digest(1), auth[other])


@pytest.mark.parametrize(
    "age",
    [None, NOW, NOW + timedelta(days=1), NOW - timedelta(hours=167), OLD.replace(tzinfo=None)],
)
def test_unknown_young_future_or_naive_ages_protected(monkeypatch, age):
    _, delete = setup_gc(monkeypatch, inventory([(tag("4230.2.0"), age)]))
    run({**SPEC, "retention": {"enabled": True}})
    delete.assert_not_called()


def test_age_boundary_and_no_node_baseline(monkeypatch):
    _, delete = setup_gc(monkeypatch, inventory([(tag("4230.2.0"), NOW - timedelta(hours=168))]))
    result = run({**SPEC, "flatcar": {"versions": ["4757.2.0"]}}, {})
    delete.assert_called_once()
    assert result["retainedFlatcarVersions"] == ["4757.2.0"]


@pytest.mark.parametrize(
    "name",
    [
        "latest",
        "junk-flatcarbroken",
        "junk-flatcar4230.2.0",
        "550.54.15-flatcar4230",
        "550.54.15-flatcar4230.2.0-invalid_suffix",
    ],
)
def test_unrelated_and_malformed_tags_preserved(monkeypatch, name):
    _, delete = setup_gc(monkeypatch, inventory([(name, OLD)]))
    run()
    delete.assert_not_called()


def test_protected_alias_blocks_digest(monkeypatch):
    inv = inventory([(tag("4230.2.0"), OLD, digest(1)), ("latest", OLD, digest(1))])
    _, delete = setup_gc(monkeypatch, inv)
    result = run({**SPEC, "retention": {"enabled": True}})
    delete.assert_not_called()
    assert result["retention"]["candidateCount"] == 1
    assert result["retention"]["skippedCount"] == 2


def test_eligible_aliases_deleted_once_and_all_recorded(monkeypatch):
    inv = inventory([(tag("4230.2.0"), OLD, digest(1)), (tag("4230.2.1"), OLD, digest(1))])
    _, delete = setup_gc(monkeypatch, inv)
    result = run({**SPEC, "retention": {"enabled": True}})
    delete.assert_called_once()
    assert len(result["pruned"]) == 2


def test_nested_protected_index_blocks_child(monkeypatch):
    inv = inventory([(tag("4230.2.0"), OLD, digest(1)), ("latest", OLD, digest(3))])
    inv.manifests[digest(3)] = ManifestInfo(digest(3), (digest(2),), OLD)
    inv.manifests[digest(2)] = ManifestInfo(digest(2), (digest(1),), OLD)
    _, delete = setup_gc(monkeypatch, inv)
    run({**SPEC, "retention": {"enabled": True}})
    delete.assert_not_called()


@pytest.mark.parametrize(
    "error,reason",
    [
        (RegistryError("dangling manifest"), "InventoryIncomplete"),
        (RegistryUnreachable("offline"), "RegistryUnreachable"),
    ],
)
def test_incomplete_inventory_never_deletes(monkeypatch, error, reason):
    read, delete = setup_gc(monkeypatch, inventory([]))
    read.side_effect = error
    result = run()
    assert result["retention"]["reason"] == reason
    assert result["retention"]["result"] == "Failed"
    delete.assert_not_called()


def test_all_repositories_validated_before_deletion(monkeypatch):
    read, delete = setup_gc(monkeypatch, inventory([]))
    read.side_effect = [inventory([(tag("4230.2.0"), OLD)]), RegistryError("403")]
    result = run(
        {
            **SPEC,
            "precompile": True,
            "registry": {"repository": REPO, "repositoryPrecompiled": "other.example.com/images"},
        }
    )
    assert result["retention"]["result"] == "Failed"
    delete.assert_not_called()


def test_revalidation_failure_or_change_blocks_deletion(monkeypatch):
    inv = inventory([(tag("4230.2.0"), OLD)])
    read, delete = setup_gc(monkeypatch, inv)
    read.side_effect = [inv, inventory([])]
    assert run()["retention"]["reason"] == "InventoryChanged"
    delete.assert_not_called()
    read.side_effect = [inv, RegistryError("403")]
    assert run()["retention"]["result"] == "Failed"
    delete.assert_not_called()


@pytest.mark.parametrize(
    "error,reason",
    [
        (TagDeletionDisabled("405"), "RegistryDeleteDisabled"),
        (RegistryError("500"), "RegistryDeleteFailed"),
    ],
)
def test_partial_failure_preserves_successes_and_stops(monkeypatch, error, reason):
    inv = inventory([(tag(f"4230.2.{n}"), OLD) for n in range(3)])
    _, delete = setup_gc(monkeypatch, inv)
    delete.side_effect = [None, error]
    result = run({**SPEC, "retention": {"enabled": True}})
    assert result["retention"]["result"] == "PartialFailure"
    assert result["retention"]["reason"] == reason
    assert len(result["pruned"]) == 1
    assert delete.call_count == 2


def test_disabled_and_no_versions_preserve_attempt_time(monkeypatch):
    read, delete = setup_gc(monkeypatch, inventory([]))
    status = {"retention": {"lastAttemptTime": "earlier"}}
    for spec, reason in [({**SPEC, "retention": {}}, "Disabled"), (SPEC, "NoFlatcarVersions")]:
        result = run(spec, status)
        assert result["retention"]["reason"] == reason
        assert result["retention"]["lastAttemptTime"] == "earlier"
    read.assert_not_called()
    delete.assert_not_called()


def test_history_capped(monkeypatch):
    setup_gc(monkeypatch, inventory([(tag("4230.2.0"), OLD)]))
    result = run(
        {**SPEC, "retention": {"enabled": True}},
        {**STATUS, "pruned": [{"tag": f"old:{n}"} for n in range(100)]},
    )
    assert len(result["pruned"]) == 100
    assert result["pruned"][0]["tag"] == REPO + ":" + tag("4230.2.0")



def test_configured_precompiled_repository_retained_after_mode_change(monkeypatch):
    read, _ = setup_gc(monkeypatch, inventory([]))
    run({**SPEC, "precompile": False, "registry": {
        "repository": REPO, "repositoryPrecompiled": "other.example.com/precompiled"}})
    assert {call.args[0] for call in read.call_args_list} == {
        REPO, "other.example.com/precompiled",
    }


def test_candidate_parent_deleted_before_tagged_descendant_on_partial_failure(monkeypatch):
    inv = inventory([(tag("4230.2.0"), OLD, digest(1)),
                     (tag("4230.2.1"), OLD, digest(3))])
    inv.manifests[digest(3)] = ManifestInfo(digest(3), (digest(2),), OLD)
    inv.manifests[digest(2)] = ManifestInfo(digest(2), (digest(1),), OLD)
    _, delete = setup_gc(monkeypatch, inv)
    delete.side_effect = [None, RegistryError("500")]
    result = run({**SPEC, "retention": {"enabled": True}})
    assert [call.args[1] for call in delete.call_args_list] == [digest(3), digest(1)]
    assert result["retention"]["result"] == "PartialFailure"
    assert result["pruned"][0]["tag"].endswith(tag("4230.2.1"))


def test_delete_disabled_without_success_reports_failure(monkeypatch):
    _, delete = setup_gc(monkeypatch, inventory([(tag("4230.2.0"), OLD)]))
    delete.side_effect = TagDeletionDisabled("405")
    result = run({**SPEC, "retention": {"enabled": True}})
    assert result["retention"]["reason"] == "RegistryDeleteDisabled"
    assert result["retention"]["result"] == "Failed"
    assert result["pruned"] == []


@pytest.mark.parametrize("driver", ["550.54.15-1", "550.54.15-123.4", "550.54.15-beta", "550.54"])
@pytest.mark.parametrize("flatcar", ["4593.2.0-beta", "4593.2.0-custom.12", "4593.2.0-1"])
@pytest.mark.parametrize("kernel", ["", "-6.12.1-flatcar"])
def test_crd_valid_version_suffixes_parsed_and_pruned(monkeypatch, driver, flatcar, kernel):
    name = f"{driver}{kernel}-flatcar{flatcar}"
    key = gc.parse_image_tag(name)
    assert key is not None
    assert key.driver == driver
    assert key.flatcar == flatcar
    assert key.precompile == bool(kernel)
    _, delete = setup_gc(monkeypatch, inventory([(name, OLD)]))
    result = run({**SPEC, "retention": {"enabled": True}})
    delete.assert_called_once()
    assert result["retention"]["result"] == "Succeeded"


def test_suffixed_required_versions_and_rollback_remain_protected(monkeypatch):
    versions = ["4757.2.0-custom", "5000.2.0-beta", "4593.2.0-pin",
                "4757.2.0-10", "4757.2.0-2", "4230.2.0-legacy"]
    inv = inventory([(tag(v), OLD) for v in versions])
    _, delete = setup_gc(monkeypatch, inv)
    spec = {**SPEC, "flatcar": {"versions": ["4593.2.0-pin"]}}
    status = {"observedNodes": [{"flatcarVersion": "4757.2.0-custom"}],
              "trackedChannelVersions": [{"flatcarVersion": "5000.2.0-beta"}]}
    result = run(spec, status)
    assert result["retention"]["result"] == "Succeeded"
    assert set(result["retainedFlatcarVersions"]) == set(versions[:4])
    assert delete.call_count == 2


def test_retention_flatcar_pattern_matches_crd():
    from pathlib import Path
    import yaml
    crd = yaml.safe_load((Path(__file__).resolve().parents[2] /
        "charts/vgpu-driver-operator/crds/vgpudriverimages.vgpu.flatcar.io.yaml").read_text())
    properties = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]
    pattern = properties["spec"]["properties"]["flatcar"]["properties"]["versions"]["items"]["pattern"]
    # Both regexes intentionally implement the same accepted language.
    import re
    for version in ["4593.2.0", "4593.2.0-beta", "4593.2.0-foo.2", "4593.2.0-1",
                    "4593.2.0-.", "4593.2.0-a_b", "4593.2", "x4593.2.0", "4593.2.0-"]:
        assert bool(re.fullmatch(pattern, version)) == bool(
            re.fullmatch(gc.reconciler.FLATCAR_VERSION_PATTERN, version))
