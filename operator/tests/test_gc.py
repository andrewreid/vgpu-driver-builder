"""Tests for gc.py — parse_duration and gc.run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from vgpu_driver_operator.gc import parse_duration, run as gc_run
from vgpu_driver_operator.registry import RegistryAuth, TagDeletionDisabled

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


# ---------------------------------------------------------------------------
# gc.run — helper builders
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

_SPEC = {
    "driverVersions": ["550.54.15"],
    "precompile": False,
    "registry": {"repository": "registry.example.com/vgpu/drivers"},
    "retention": {
        "enabled": True,
        "keepPreviousFlatcarVersions": 1,
        "minAgeBeforeDelete": "168h",
    },
}

_STATUS = {
    "observedNodes": [
        {"flatcarVersion": "4081.2.1", "kernelVersion": "6.1.120"},
    ],
    "trackedChannelVersions": [
        {
            "channel": "stable",
            "flatcarVersion": "4081.2.1",
            "kernelVersion": "6.1.120",
        }
    ],
    "pruned": [],
}

_AUTH: RegistryAuth = {"username": "user", "password": "pass"}

_REPO = "registry.example.com/vgpu/drivers"

# Tags present in the registry.
# 4081.2.1 is current — must be retained.
# 4081.2.0 is older — eligible if old enough.
# 3815.2.0 is much older flatcar — eligible.
_EXISTING_TAGS = {
    "550.54.15-flatcar4081.2.1",
    "550.54.15-flatcar4081.2.0",
    "550.54.15-flatcar3815.2.0",
}

# Ages: 4081.2.0 is 8 days old (> 168h limit), 3815.2.0 is 30 days old.
_TAG_AGES = {
    "550.54.15-flatcar4081.2.1": _NOW - timedelta(days=1),
    "550.54.15-flatcar4081.2.0": _NOW - timedelta(days=8),
    "550.54.15-flatcar3815.2.0": _NOW - timedelta(days=30),
}


class TestGcRun:
    def _make_mocks(self, tags=_EXISTING_TAGS, ages=_TAG_AGES):
        list_tags_mock = MagicMock(return_value=set(tags))
        tag_created_at_mock = MagicMock(side_effect=lambda repo, tag, auth: ages.get(tag))
        delete_tag_mock = MagicMock(return_value=None)
        return list_tags_mock, tag_created_at_mock, delete_tag_mock

    @patch("vgpu_driver_operator.gc._registry.list_tags")
    @patch("vgpu_driver_operator.gc._registry.tag_created_at")
    @patch("vgpu_driver_operator.gc._registry.delete_tag")
    def test_prunes_old_tag_retains_current_and_one_previous(
        self, mock_delete, mock_created_at, mock_list_tags
    ):
        mock_list_tags.return_value = set(_EXISTING_TAGS)
        mock_created_at.side_effect = lambda repo, tag, auth: _TAG_AGES.get(tag)

        emit_calls = []

        def emit(reason, message, type_="Normal"):
            emit_calls.append((reason, message, type_))

        result = gc_run(
            spec=_SPEC,
            status=_STATUS,
            auth=_AUTH,
            now=_NOW,
            logger=MagicMock(),
            emit_event=emit,
        )

        # Shape checks.
        assert "pruned" in result
        assert "retainedFlatcarVersions" in result

        retained = set(result["retainedFlatcarVersions"])
        # 4081.2.1 is current → must be retained.
        assert "4081.2.1" in retained
        # keepPreviousFlatcarVersions=1, so 4081.2.0 (the previous version) is retained too.
        assert "4081.2.0" in retained
        # 3815.2.0 is too old to be kept under keep_previous=1.
        assert "3815.2.0" not in retained

        # Only 3815.2.0 should be deleted (4081.2.0 is retained by keep_previous=1).
        assert mock_delete.call_count == 1
        delete_args = mock_delete.call_args
        assert "3815.2.0" in delete_args[0][1]  # tag arg

        # Pruned list should have one entry.
        assert len(result["pruned"]) == 1
        assert "3815.2.0" in result["pruned"][0]["tag"]

    @patch("vgpu_driver_operator.gc._registry.list_tags")
    @patch("vgpu_driver_operator.gc._registry.tag_created_at")
    @patch("vgpu_driver_operator.gc._registry.delete_tag")
    def test_tag_deletion_disabled_emits_event_no_exception(
        self, mock_delete, mock_created_at, mock_list_tags
    ):
        mock_list_tags.return_value = {"550.54.15-flatcar3815.2.0"}
        mock_created_at.return_value = _NOW - timedelta(days=30)
        mock_delete.side_effect = TagDeletionDisabled("405 from registry")

        emit_calls: list[tuple] = []

        def emit(reason, message, type_="Normal"):
            emit_calls.append((reason, message, type_))

        # Use keep_previous=0 so 3815.2.0 is not retained and is eligible for deletion.
        spec_no_keep = dict(_SPEC)
        spec_no_keep["retention"] = dict(_SPEC["retention"])
        spec_no_keep["retention"]["keepPreviousFlatcarVersions"] = 0

        # Should not raise.
        result = gc_run(
            spec=spec_no_keep,
            status={
                "observedNodes": [{"flatcarVersion": "4081.2.1", "kernelVersion": "6.1.120"}],
                "trackedChannelVersions": [],
                "pruned": [],
            },
            auth=_AUTH,
            now=_NOW,
            logger=MagicMock(),
            emit_event=emit,
        )

        # Event emitted with the right reason.
        assert any(r == "RegistryDeleteDisabled" for r, *_ in emit_calls)
        # No pruned entries (deletion was blocked).
        assert result["pruned"] == []

    @patch("vgpu_driver_operator.gc._registry.list_tags")
    @patch("vgpu_driver_operator.gc._registry.tag_created_at")
    @patch("vgpu_driver_operator.gc._registry.delete_tag")
    def test_tag_too_young_not_deleted(
        self, mock_delete, mock_created_at, mock_list_tags
    ):
        """A tag that is only 1 day old should not be deleted (min age 168h)."""
        mock_list_tags.return_value = {"550.54.15-flatcar3815.2.0"}
        mock_created_at.return_value = _NOW - timedelta(hours=1)  # < 168h

        gc_run(
            spec=_SPEC,
            status={
                "observedNodes": [{"flatcarVersion": "4081.2.1", "kernelVersion": "6.1.120"}],
                "trackedChannelVersions": [],
                "pruned": [],
            },
            auth=_AUTH,
            now=_NOW,
            logger=MagicMock(),
            emit_event=MagicMock(),
        )

        mock_delete.assert_not_called()

    @patch("vgpu_driver_operator.gc._registry.list_tags")
    @patch("vgpu_driver_operator.gc._registry.tag_created_at")
    @patch("vgpu_driver_operator.gc._registry.delete_tag")
    def test_registry_error_on_delete_logs_and_continues(
        self, mock_delete, mock_created_at, mock_list_tags
    ):
        """Non-405 RegistryError on delete should be logged but not stop GC."""
        from vgpu_driver_operator.registry import RegistryError

        mock_list_tags.return_value = {"550.54.15-flatcar3815.2.0"}
        mock_created_at.return_value = _NOW - timedelta(days=30)
        mock_delete.side_effect = RegistryError("500 server error")

        spec_no_keep = dict(_SPEC)
        spec_no_keep["retention"] = dict(_SPEC["retention"])
        spec_no_keep["retention"]["keepPreviousFlatcarVersions"] = 0

        # Should not raise.
        result = gc_run(
            spec=spec_no_keep,
            status={
                "observedNodes": [{"flatcarVersion": "4081.2.1", "kernelVersion": "6.1.120"}],
                "trackedChannelVersions": [],
                "pruned": [],
            },
            auth=_AUTH,
            now=_NOW,
            logger=MagicMock(),
            emit_event=MagicMock(),
        )
        # Nothing pruned (delete raised RegistryError).
        assert result["pruned"] == []

    @patch("vgpu_driver_operator.gc._registry.list_tags")
    @patch("vgpu_driver_operator.gc._registry.tag_created_at")
    @patch("vgpu_driver_operator.gc._registry.delete_tag")
    def test_list_tags_failure_continues(
        self, mock_delete, mock_created_at, mock_list_tags
    ):
        """If list_tags raises RegistryError, GC continues with an empty tag set."""
        from vgpu_driver_operator.registry import RegistryError

        mock_list_tags.side_effect = RegistryError("network error")

        result = gc_run(
            spec=_SPEC,
            status={
                "observedNodes": [{"flatcarVersion": "4081.2.1", "kernelVersion": "6.1.120"}],
                "trackedChannelVersions": [],
                "pruned": [],
            },
            auth=_AUTH,
            now=_NOW,
            logger=MagicMock(),
            emit_event=MagicMock(),
        )
        assert result["pruned"] == []
        mock_delete.assert_not_called()

    @patch("vgpu_driver_operator.gc._registry.list_tags")
    @patch("vgpu_driver_operator.gc._registry.tag_created_at")
    @patch("vgpu_driver_operator.gc._registry.delete_tag")
    def test_pruned_history_preserved_and_capped(
        self, mock_delete, mock_created_at, mock_list_tags
    ):
        """Previously-pruned entries are merged and capped at 100."""
        mock_list_tags.return_value = {"550.54.15-flatcar3815.2.0"}
        mock_created_at.return_value = _NOW - timedelta(days=30)

        old_pruned = [
            {"tag": f"repo:tag{i}", "reason": "RetentionPolicy", "prunedAt": "2025-01-01"}
            for i in range(99)
        ]

        # Use keep_previous=0 so 3815.2.0 is not retained and gets deleted.
        spec_no_keep = dict(_SPEC)
        spec_no_keep["retention"] = dict(_SPEC["retention"])
        spec_no_keep["retention"]["keepPreviousFlatcarVersions"] = 0

        result = gc_run(
            spec=spec_no_keep,
            status={
                "observedNodes": [{"flatcarVersion": "4081.2.1", "kernelVersion": "6.1.120"}],
                "trackedChannelVersions": [],
                "pruned": old_pruned,
            },
            auth=_AUTH,
            now=_NOW,
            logger=MagicMock(),
            emit_event=MagicMock(),
        )

        # 1 new + 99 old = 100, all fit within the cap.
        assert len(result["pruned"]) == 100
