"""Tests for vgpu_driver_operator.reconciler."""

from datetime import datetime, timedelta, timezone

import pytest

from vgpu_driver_operator.reconciler import (
    BuildKey,
    compute_desired,
    compute_missing,
    compute_prunable,
    compute_retained_flatcar_set,
    parse_precompile_tag,
    parse_runtime_tag,
    precompile_tag,
    runtime_tag,
)

# ---------------------------------------------------------------------------
# compute_desired
# ---------------------------------------------------------------------------


class TestComputeDesiredRuntime:
    def test_two_drivers_three_flatcars(self):
        drivers = ["550.54.15", "535.183.01"]
        node_pairs = {
            ("4230.2.3", "6.1.119"),
            ("4081.2.0", "6.1.84"),
            ("3815.2.0", "5.15.126"),
        }
        tracked_pairs: set[tuple[str, str]] = set()
        result = compute_desired(
            drivers, node_pairs, tracked_pairs, precompile=False
        )
        assert len(result) == 6
        # All kernel fields should be None in runtime mode.
        assert all(k.kernel is None for k in result)
        # Each driver × each flatcar should be present.
        driver_flatcars = {(k.driver, k.flatcar) for k in result}
        assert ("550.54.15", "4230.2.3") in driver_flatcars
        assert ("535.183.01", "3815.2.0") in driver_flatcars

    def test_node_and_tracked_merged(self):
        drivers = ["550.54.15"]
        node_pairs = {("4230.2.3", "6.1.119")}
        tracked_pairs = {("4230.2.4", "6.1.120")}  # newer from poller
        result = compute_desired(
            drivers, node_pairs, tracked_pairs, precompile=False
        )
        flatcars = {k.flatcar for k in result}
        assert "4230.2.3" in flatcars
        assert "4230.2.4" in flatcars

    def test_duplicate_flatcar_deduped(self):
        """Same flatcar version from both node and tracked → 1 key per driver."""
        drivers = ["550.54.15"]
        node_pairs = {("4230.2.3", "6.1.119")}
        tracked_pairs = {("4230.2.3", "6.1.119")}
        result = compute_desired(
            drivers, node_pairs, tracked_pairs, precompile=False
        )
        assert len(result) == 1


class TestComputeDesiredPrecompile:
    def test_two_drivers_two_pairs(self):
        drivers = ["550.54.15", "535.183.01"]
        node_pairs = {
            ("4230.2.3", "6.1.119"),
            ("4081.2.0", "6.1.84"),
        }
        tracked_pairs: set[tuple[str, str]] = set()
        result = compute_desired(
            drivers, node_pairs, tracked_pairs, precompile=True
        )
        assert len(result) == 4
        # All kernel fields must be set.
        assert all(k.kernel is not None for k in result)
        for driver in drivers:
            assert BuildKey(driver, "4230.2.3", "6.1.119") in result
            assert BuildKey(driver, "4081.2.0", "6.1.84") in result

    def test_same_flatcar_different_kernel_kept_separately(self):
        """In precompile mode, same flatcar but different kernels → 2 keys."""
        drivers = ["550.54.15"]
        node_pairs = {("4230.2.3", "6.1.119")}
        tracked_pairs = {("4230.2.3", "6.1.120")}
        result = compute_desired(
            drivers, node_pairs, tracked_pairs, precompile=True
        )
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Tag round-trips
# ---------------------------------------------------------------------------


RUNTIME_TAG_CASES = [
    ("550.54.15", "4230.2.3"),
    ("535.183.01", "3815.2.0"),
    ("520.0.0", "4081.2.0"),
    ("470.82.01", "4152.2.0"),
    ("390.157", "3941.0.0"),
]

PRECOMPILE_TAG_CASES = [
    ("550.54.15", "4230.2.3", "6.1.119"),
    ("535.183.01", "3815.2.0", "5.15.126"),
    ("520.0.0", "4081.2.0", "6.1.84"),
    ("470.82.01", "4152.2.0", "6.1.55"),
    ("390.157", "3941.0.0", "5.15.0"),
]


class TestTagRoundTrips:
    @pytest.mark.parametrize("driver,flatcar", RUNTIME_TAG_CASES)
    def test_runtime_roundtrip(self, driver, flatcar):
        key = BuildKey(driver=driver, flatcar=flatcar, kernel=None)
        tag = runtime_tag(key)
        recovered = parse_runtime_tag(tag)
        assert recovered == key

    @pytest.mark.parametrize("driver,flatcar,kernel", PRECOMPILE_TAG_CASES)
    def test_precompile_roundtrip(self, driver, flatcar, kernel):
        key = BuildKey(driver=driver, flatcar=flatcar, kernel=kernel)
        tag = precompile_tag(key)
        recovered = parse_precompile_tag(tag)
        assert recovered == key

    def test_runtime_parse_invalid_returns_none(self):
        assert parse_runtime_tag("notavalidtag") is None
        assert parse_runtime_tag("") is None
        assert parse_runtime_tag("flatcar4230.2.3") is None

    def test_precompile_parse_invalid_returns_none(self):
        assert parse_precompile_tag("notavalidtag") is None
        assert parse_precompile_tag("") is None

    def test_precompile_tag_requires_kernel(self):
        key = BuildKey(driver="550.54.15", flatcar="4230.2.3", kernel=None)
        with pytest.raises(ValueError):
            precompile_tag(key)


# ---------------------------------------------------------------------------
# compute_missing
# ---------------------------------------------------------------------------


class TestComputeMissing:
    def test_filters_existing_tags(self):
        desired = {
            BuildKey("550.54.15", "4230.2.3"),
            BuildKey("550.54.15", "4081.2.0"),
        }
        existing = {"550.54.15-flatcar4230.2.3"}
        missing = compute_missing(desired, existing, set(), precompile=False)
        assert missing == {BuildKey("550.54.15", "4081.2.0")}

    def test_filters_in_flight(self):
        desired = {BuildKey("550.54.15", "4230.2.3")}
        in_flight = {BuildKey("550.54.15", "4230.2.3")}
        missing = compute_missing(desired, set(), in_flight, precompile=False)
        assert missing == set()

    def test_all_missing(self):
        desired = {
            BuildKey("550.54.15", "4230.2.3"),
            BuildKey("550.54.15", "4081.2.0"),
        }
        missing = compute_missing(desired, set(), set(), precompile=False)
        assert missing == desired

    def test_precompile_mode(self):
        key = BuildKey("550.54.15", "4230.2.3", "6.1.119")
        tag = precompile_tag(key)
        desired = {key}
        # Already built.
        missing = compute_missing(desired, {tag}, set(), precompile=True)
        assert missing == set()
        # Not yet built.
        missing = compute_missing(desired, set(), set(), precompile=True)
        assert missing == {key}


# ---------------------------------------------------------------------------
# compute_retained_flatcar_set
# ---------------------------------------------------------------------------


class TestComputeRetainedFlatcarSet:
    def test_plan_example(self):
        """The worked example from the spec:
        nodes {4230.2.3}, tracked {4230.2.4},
        history {4230.2.2, 4152.2.0, 4081.2.0}, keep=2
        → {4230.2.4, 4230.2.3, 4230.2.2, 4152.2.0}
        """
        result = compute_retained_flatcar_set(
            current={"4230.2.3"},
            tracked={"4230.2.4"},
            history={"4230.2.2", "4152.2.0", "4081.2.0"},
            keep_previous=2,
        )
        assert result == {"4230.2.4", "4230.2.3", "4230.2.2", "4152.2.0"}

    def test_keep_zero_returns_current_plus_tracked(self):
        result = compute_retained_flatcar_set(
            current={"4230.2.3"},
            tracked={"4230.2.4"},
            history={"4230.2.2", "4152.2.0", "4081.2.0"},
            keep_previous=0,
        )
        assert result == {"4230.2.3", "4230.2.4"}

    def test_empty_current(self):
        result = compute_retained_flatcar_set(
            current=set(),
            tracked={"4230.2.4"},
            history={"4230.2.2"},
            keep_previous=2,
        )
        # No highest current → no history added.
        assert result == {"4230.2.4"}

    def test_keep_more_than_available(self):
        """keep_previous > number of older versions → return all older."""
        result = compute_retained_flatcar_set(
            current={"4230.2.3"},
            tracked=set(),
            history={"4230.2.1"},
            keep_previous=5,
        )
        assert "4230.2.1" in result

    def test_history_versions_above_current_not_retained(self):
        """History versions newer than current node version are excluded."""
        result = compute_retained_flatcar_set(
            current={"4230.2.3"},
            tracked=set(),
            history={"4230.2.4", "4230.2.2"},
            keep_previous=2,
        )
        # 4230.2.4 is newer than current 4230.2.3, should not be in retained
        # (it's not in history candidates, only current/tracked add those).
        assert "4230.2.4" not in result
        assert "4230.2.2" in result


# ---------------------------------------------------------------------------
# compute_prunable
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
_MIN_AGE = timedelta(hours=168)  # 7 days


def _old_ts():
    return _NOW - timedelta(hours=200)


def _young_ts():
    return _NOW - timedelta(hours=10)


class TestComputePrunable:
    def test_tag_unknown_age_never_prunable(self):
        existing = {"550.54.15-flatcar4081.2.0"}
        tag_ages = {"550.54.15-flatcar4081.2.0": None}
        retained = set()
        result = compute_prunable(
            existing, tag_ages, retained, _MIN_AGE, now=_NOW, precompile=False
        )
        assert result == set()

    def test_tag_in_retained_never_prunable(self):
        existing = {"550.54.15-flatcar4230.2.3"}
        tag_ages = {"550.54.15-flatcar4230.2.3": _old_ts()}
        retained = {"4230.2.3"}
        result = compute_prunable(
            existing, tag_ages, retained, _MIN_AGE, now=_NOW, precompile=False
        )
        assert result == set()

    def test_tag_too_young_never_prunable(self):
        existing = {"550.54.15-flatcar4081.2.0"}
        tag_ages = {"550.54.15-flatcar4081.2.0": _young_ts()}
        retained = set()
        result = compute_prunable(
            existing, tag_ages, retained, _MIN_AGE, now=_NOW, precompile=False
        )
        assert result == set()

    def test_prunable_tag(self):
        existing = {"550.54.15-flatcar4081.2.0"}
        tag_ages = {"550.54.15-flatcar4081.2.0": _old_ts()}
        retained = set()
        result = compute_prunable(
            existing, tag_ages, retained, _MIN_AGE, now=_NOW, precompile=False
        )
        assert result == {"550.54.15-flatcar4081.2.0"}

    def test_mixed_tags(self):
        retained_tag = "550.54.15-flatcar4230.2.3"
        old_tag = "550.54.15-flatcar4081.2.0"
        young_tag = "550.54.15-flatcar3815.2.0"
        unknown_age_tag = "550.54.15-flatcar3600.0.0"
        existing = {retained_tag, old_tag, young_tag, unknown_age_tag}
        tag_ages = {
            retained_tag: _old_ts(),
            old_tag: _old_ts(),
            young_tag: _young_ts(),
            unknown_age_tag: None,
        }
        retained = {"4230.2.3"}
        result = compute_prunable(
            existing, tag_ages, retained, _MIN_AGE, now=_NOW, precompile=False
        )
        assert result == {old_tag}

    def test_precompile_mode(self):
        key = BuildKey("550.54.15", "4081.2.0", "6.1.84")
        tag = precompile_tag(key)
        existing = {tag}
        tag_ages = {tag: _old_ts()}
        retained = set()
        result = compute_prunable(
            existing, tag_ages, retained, _MIN_AGE, now=_NOW, precompile=True
        )
        assert result == {tag}
