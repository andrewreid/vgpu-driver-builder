"""Tests for vgpu_driver_operator.reconciler."""

from datetime import datetime, timedelta, timezone

import pytest

from vgpu_driver_operator.reconciler import (
    BuildKey,
    compute_desired,
    compute_missing,
    compute_prunable,
    compute_retained_flatcar_set,
    extract_kernel_from_precompile_tag,
    matches_precompile_tag,
    parse_precompile_tag,
    parse_runtime_tag,
    runtime_tag,
)

# ---------------------------------------------------------------------------
# compute_desired
# ---------------------------------------------------------------------------


class TestComputeDesiredExplicitVersions:
    def test_explicit_versions_runtime(self):
        """explicit_versions contributes flatcar versions in runtime mode."""
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers,
            node_flatcars=set(),
            tracked_flatcars=set(),
            precompile=False,
            explicit_versions=["4593.2.0"],
        )
        assert result == {BuildKey(driver="550.54.15", flatcar="4593.2.0", precompile=False)}

    def test_explicit_versions_dedup_with_tracked(self):
        """Same flatcar from explicit + tracked → one key per driver in runtime."""
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers,
            node_flatcars=set(),
            tracked_flatcars={"4593.2.0"},
            precompile=False,
            explicit_versions=["4593.2.0"],
        )
        assert len(result) == 1

    def test_explicit_versions_union_with_tracked(self):
        """Explicit + tracked are unioned, not intersected."""
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers,
            node_flatcars=set(),
            tracked_flatcars={"4230.2.3"},
            precompile=False,
            explicit_versions=["4593.2.0"],
        )
        flatcars = {k.flatcar for k in result}
        assert "4230.2.3" in flatcars
        assert "4593.2.0" in flatcars

    def test_explicit_versions_precompile(self):
        """Explicit version in precompile mode → precompile=True key."""
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers,
            node_flatcars=set(),
            tracked_flatcars=set(),
            precompile=True,
            explicit_versions=["4593.2.0"],
        )
        assert BuildKey(driver="550.54.15", flatcar="4593.2.0", precompile=True) in result

    def test_empty_explicit_versions_no_effect(self):
        """empty explicit_versions → same as not passing it."""
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers,
            node_flatcars={"4230.2.3"},
            tracked_flatcars=set(),
            precompile=False,
            explicit_versions=[],
        )
        assert result == {BuildKey(driver="550.54.15", flatcar="4230.2.3", precompile=False)}

    def test_none_explicit_versions_no_effect(self):
        """explicit_versions=None is equivalent to empty list."""
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers,
            node_flatcars={"4230.2.3"},
            tracked_flatcars=set(),
            precompile=False,
            explicit_versions=None,
        )
        assert result == {BuildKey(driver="550.54.15", flatcar="4230.2.3", precompile=False)}

    def test_precompile_explicit_produces_build_job(self):
        """Bug 13 regression: explicit flatcar.versions with precompile=True must
        produce a build key — never silently fall back to runtime mode."""
        drivers = ["535.261.03"]
        result = compute_desired(
            drivers,
            node_flatcars=set(),
            tracked_flatcars=set(),
            precompile=True,
            explicit_versions=["4593.2.0"],
        )
        assert len(result) == 1
        key = next(iter(result))
        assert key.driver == "535.261.03"
        assert key.flatcar == "4593.2.0"
        assert key.precompile is True


class TestComputeDesiredRuntime:
    def test_two_drivers_three_flatcars(self):
        drivers = ["550.54.15", "535.183.01"]
        node_flatcars = {"4230.2.3", "4081.2.0", "3815.2.0"}
        result = compute_desired(
            drivers, node_flatcars, set(), precompile=False
        )
        assert len(result) == 6
        assert all(k.precompile is False for k in result)
        driver_flatcars = {(k.driver, k.flatcar) for k in result}
        assert ("550.54.15", "4230.2.3") in driver_flatcars
        assert ("535.183.01", "3815.2.0") in driver_flatcars

    def test_node_and_tracked_merged(self):
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers, {"4230.2.3"}, {"4230.2.4"}, precompile=False
        )
        flatcars = {k.flatcar for k in result}
        assert "4230.2.3" in flatcars
        assert "4230.2.4" in flatcars

    def test_duplicate_flatcar_deduped(self):
        """Same flatcar from both node and tracked → 1 key per driver."""
        drivers = ["550.54.15"]
        result = compute_desired(
            drivers, {"4230.2.3"}, {"4230.2.3"}, precompile=False
        )
        assert len(result) == 1


class TestComputeDesiredPrecompile:
    def test_two_drivers_two_flatcars(self):
        drivers = ["550.54.15", "535.183.01"]
        node_flatcars = {"4230.2.3", "4081.2.0"}
        result = compute_desired(
            drivers, node_flatcars, set(), precompile=True
        )
        assert len(result) == 4
        assert all(k.precompile is True for k in result)
        for driver in drivers:
            assert BuildKey(driver, "4230.2.3", True) in result
            assert BuildKey(driver, "4081.2.0", True) in result


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
        key = BuildKey(driver=driver, flatcar=flatcar, precompile=False)
        tag = runtime_tag(key)
        recovered = parse_runtime_tag(tag)
        assert recovered == key

    @pytest.mark.parametrize("driver,flatcar,kernel", PRECOMPILE_TAG_CASES)
    def test_precompile_parse(self, driver, flatcar, kernel):
        tag = f"{driver}-{kernel}-flatcar{flatcar}"
        recovered = parse_precompile_tag(tag)
        assert recovered is not None
        assert recovered.driver == driver
        assert recovered.flatcar == flatcar
        assert recovered.precompile is True

    @pytest.mark.parametrize("driver,flatcar,kernel", PRECOMPILE_TAG_CASES)
    def test_matches_precompile_tag(self, driver, flatcar, kernel):
        tag = f"{driver}-{kernel}-flatcar{flatcar}"
        key = BuildKey(driver=driver, flatcar=flatcar, precompile=True)
        assert matches_precompile_tag(key, tag)

    @pytest.mark.parametrize("driver,flatcar,kernel", PRECOMPILE_TAG_CASES)
    def test_extract_kernel_from_precompile_tag(self, driver, flatcar, kernel):
        tag = f"{driver}-{kernel}-flatcar{flatcar}"
        assert extract_kernel_from_precompile_tag(tag) == kernel

    def test_runtime_parse_invalid_returns_none(self):
        assert parse_runtime_tag("notavalidtag") is None
        assert parse_runtime_tag("") is None
        assert parse_runtime_tag("flatcar4230.2.3") is None

    def test_precompile_parse_invalid_returns_none(self):
        assert parse_precompile_tag("notavalidtag") is None
        assert parse_precompile_tag("") is None

    def test_precompile_driver_with_rc_suffix(self):
        """Driver version with rc suffix like 535.104-rc1 must parse correctly."""
        tag = "535.104-rc1-6.12.81-flatcar4593.2.0"
        recovered = parse_precompile_tag(tag)
        assert recovered is not None
        assert recovered.driver == "535.104-rc1"
        assert recovered.flatcar == "4593.2.0"
        assert recovered.precompile is True

    def test_extract_kernel_with_rc_driver(self):
        """Extract kernel from tag with rc-suffixed driver."""
        tag = "535.104-rc1-6.12.81-flatcar4593.2.0"
        assert extract_kernel_from_precompile_tag(tag) == "6.12.81"

    def test_precompile_rejects_runtime_tag_with_rc_driver(self):
        """Runtime-style tag for an rc driver must NOT parse as precompile.

        Regression: previous regex `^(driver)-(.+)$` allowed kernel="rc1"
        for tag "535.104-rc1-flatcar4593.2.0", causing false positives in
        compute_missing when same repo holds runtime + precompile tags.
        """
        assert parse_precompile_tag("535.104-rc1-flatcar4593.2.0") is None
        assert extract_kernel_from_precompile_tag("535.104-rc1-flatcar4593.2.0") is None

    def test_matches_precompile_tag_with_rc_driver(self):
        """matches_precompile_tag should work with rc-suffixed drivers."""
        tag = "535.104-rc1-6.12.81-flatcar4593.2.0"
        key = BuildKey(driver="535.104-rc1", flatcar="4593.2.0", precompile=True)
        assert matches_precompile_tag(key, tag)

    def test_matches_precompile_tag_wrong_driver(self):
        key = BuildKey(driver="550.54.15", flatcar="4230.2.3", precompile=True)
        tag = "535.183.01-6.1.119-flatcar4230.2.3"
        assert not matches_precompile_tag(key, tag)

    def test_matches_precompile_tag_wrong_flatcar(self):
        key = BuildKey(driver="550.54.15", flatcar="4230.2.3", precompile=True)
        tag = "550.54.15-6.1.119-flatcar4081.2.0"
        assert not matches_precompile_tag(key, tag)


# ---------------------------------------------------------------------------
# compute_missing
# ---------------------------------------------------------------------------


class TestComputeMissing:
    def test_filters_existing_runtime_tags(self):
        desired = {
            BuildKey("550.54.15", "4230.2.3", False),
            BuildKey("550.54.15", "4081.2.0", False),
        }
        existing = {"550.54.15-flatcar4230.2.3"}
        missing = compute_missing(desired, existing, set(), precompile=False)
        assert missing == {BuildKey("550.54.15", "4081.2.0", False)}

    def test_filters_in_flight(self):
        desired = {BuildKey("550.54.15", "4230.2.3", False)}
        in_flight = {BuildKey("550.54.15", "4230.2.3", False)}
        missing = compute_missing(desired, set(), in_flight, precompile=False)
        assert missing == set()

    def test_all_missing(self):
        desired = {
            BuildKey("550.54.15", "4230.2.3", False),
            BuildKey("550.54.15", "4081.2.0", False),
        }
        missing = compute_missing(desired, set(), set(), precompile=False)
        assert missing == desired

    def test_precompile_existing_tag_skips_build(self):
        key = BuildKey("550.54.15", "4230.2.3", True)
        # Tag contains kernel version discovered at build time.
        existing = {"550.54.15-6.1.119-flatcar4230.2.3"}
        desired = {key}
        missing = compute_missing(desired, existing, set(), precompile=True)
        assert missing == set()

    def test_precompile_no_existing_tag_needs_build(self):
        key = BuildKey("550.54.15", "4230.2.3", True)
        desired = {key}
        missing = compute_missing(desired, set(), set(), precompile=True)
        assert missing == {key}


# ---------------------------------------------------------------------------
# compute_retained_flatcar_set
# ---------------------------------------------------------------------------


class TestComputeRetainedFlatcarSet:
    def test_plan_example(self):
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
        assert result == {"4230.2.4"}

    def test_keep_more_than_available(self):
        result = compute_retained_flatcar_set(
            current={"4230.2.3"},
            tracked=set(),
            history={"4230.2.1"},
            keep_previous=5,
        )
        assert "4230.2.1" in result

    def test_history_versions_above_current_not_retained(self):
        result = compute_retained_flatcar_set(
            current={"4230.2.3"},
            tracked=set(),
            history={"4230.2.4", "4230.2.2"},
            keep_previous=2,
        )
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
        tag = "550.54.15-6.1.84-flatcar4081.2.0"
        existing = {tag}
        tag_ages = {tag: _old_ts()}
        retained = set()
        result = compute_prunable(
            existing, tag_ages, retained, _MIN_AGE, now=_NOW, precompile=True
        )
        assert result == {tag}
