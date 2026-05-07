"""VGPUDriverImage reconciler — pure logic, no Kubernetes client imports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Semver-aware comparator that handles Flatcar's MAJOR.MINOR.PATCH scheme.
# Uses ``packaging.version.Version`` when available; falls back to a simple
# tuple-of-ints comparator that is sufficient for the ``\d+\.\d+\.\d+`` form.
# ---------------------------------------------------------------------------

try:
    from packaging.version import Version as _Version  # type: ignore[import-untyped]

    def _ver(v: str) -> object:  # type: ignore[return]
        return _Version(v)

except ImportError:  # pragma: no cover

    def _ver(v: str) -> tuple[int, ...]:  # type: ignore[misc]
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)


# ---------------------------------------------------------------------------
# BuildKey
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildKey:
    """Identifies one (driver, flatcar, mode) build combination.

    The operator no longer resolves or stores kernel version — the build job
    auto-discovers it from the flatcar-sources base image at build time.
    """

    driver: str
    flatcar: str
    precompile: bool = False


# ---------------------------------------------------------------------------
# Tag formatters / parsers
# ---------------------------------------------------------------------------

# Runtime tag:    "<driver>-flatcar<flatcar>"
# Precompile tag: "<driver>-<kernel>-flatcar<flatcar>"  (kernel discovered at build time)
#
# The operator uses the runtime tag for exact-match idempotency checks.
# For precompile mode, idempotency is checked via a prefix/pattern search
# against the registry (any tag matching "<driver>-*-flatcar<flatcar>").


def runtime_tag(key: BuildKey) -> str:
    """Return the image tag for a runtime (non-precompiled) build."""
    return f"{key.driver}-flatcar{key.flatcar}"


def precompile_tag_prefix(key: BuildKey) -> str:
    """Return the tag prefix used to detect existing precompile builds.

    The actual tag is ``<driver>-<kernel>-flatcar<flatcar>`` where kernel is
    discovered by the build job.  This prefix (``<driver>-``) combined with
    the suffix (``-flatcar<flatcar>``) identifies matching tags.
    """
    return f"{key.driver}-"


def precompile_tag_suffix(key: BuildKey) -> str:
    """Return the tag suffix used to detect existing precompile builds."""
    return f"-flatcar{key.flatcar}"


def matches_precompile_tag(key: BuildKey, tag: str) -> bool:
    """Return True if *tag* matches the expected precompile pattern for *key*.

    Pattern: ``<driver>-<anything>-flatcar<flatcar>``
    """
    prefix = precompile_tag_prefix(key)
    suffix = precompile_tag_suffix(key)
    return tag.startswith(prefix) and tag.endswith(suffix) and len(tag) > len(prefix) + len(suffix)


def parse_runtime_tag(tag: str) -> BuildKey | None:
    """Parse a runtime tag back to a ``BuildKey``.

    Returns ``None`` if *tag* does not match the expected format.
    """
    sep = "-flatcar"
    idx = tag.rfind(sep)
    if idx < 1:
        return None
    driver = tag[:idx]
    flatcar = tag[idx + len(sep):]
    if not driver or not flatcar:
        return None
    return BuildKey(driver=driver, flatcar=flatcar, precompile=False)


def parse_precompile_tag(tag: str) -> BuildKey | None:
    """Parse a precompile tag back to a ``BuildKey`` (without kernel).

    Returns ``None`` if *tag* does not match the expected format.
    Pattern: ``<driver>-<kernel>-flatcar<flatcar>``
    """
    sep = "-flatcar"
    idx = tag.rfind(sep)
    if idx < 1:
        return None
    flatcar = tag[idx + len(sep):]
    remainder = tag[:idx]  # "<driver>-<kernel>"
    # Split remainder into driver and kernel: split on the FIRST "-" that is
    # preceded by a digit and followed by a digit (version boundary).
    m = re.search(r"(\d)-(\d)", remainder)
    if not m:
        return None
    split_pos = m.start() + 1  # position of the "-"
    driver = remainder[:split_pos]
    if not driver or not flatcar:
        return None
    return BuildKey(driver=driver, flatcar=flatcar, precompile=True)


def extract_kernel_from_precompile_tag(tag: str) -> str | None:
    """Extract the kernel version embedded in a precompile tag.

    Returns ``None`` if the tag does not match the precompile pattern.
    """
    sep = "-flatcar"
    idx = tag.rfind(sep)
    if idx < 1:
        return None
    remainder = tag[:idx]  # "<driver>-<kernel>"
    m = re.search(r"(\d)-(\d)", remainder)
    if not m:
        return None
    split_pos = m.start() + 1
    kernel = remainder[split_pos + 1:]
    return kernel or None


# ---------------------------------------------------------------------------
# compute_desired
# ---------------------------------------------------------------------------


def compute_desired(
    driver_versions: list[str],
    node_flatcars: set[str],
    tracked_flatcars: set[str],
    *,
    precompile: bool,
    explicit_versions: list[str] | None = None,
) -> set[BuildKey]:
    """Compute the complete set of build keys that *should* exist.

    Parameters
    ----------
    driver_versions:
        List of NVIDIA driver version strings from the CRD spec.
    node_flatcars:
        Set of Flatcar version strings observed on nodes.
        Should be empty when ``discoverFromNodes`` is false.
    tracked_flatcars:
        Set of Flatcar version strings from channel poller.
    precompile:
        When ``True`` build precompiled images; otherwise runtime images.
    explicit_versions:
        Optional list of Flatcar version strings from ``spec.flatcar.versions``.
    """
    all_flatcars: set[str] = node_flatcars | tracked_flatcars | set(explicit_versions or [])

    result: set[BuildKey] = set()
    for driver in driver_versions:
        for flatcar in all_flatcars:
            result.add(BuildKey(driver=driver, flatcar=flatcar, precompile=precompile))
    return result


# ---------------------------------------------------------------------------
# compute_missing
# ---------------------------------------------------------------------------


def compute_missing(
    desired: set[BuildKey],
    existing_tags: set[str],
    in_flight: set[BuildKey],
    *,
    precompile: bool,
) -> set[BuildKey]:
    """Return the subset of *desired* keys that have no existing tag and no
    in-flight job.
    """
    # Build set of BuildKeys that already have an image.
    built: set[BuildKey] = set()
    if precompile:
        for tag in existing_tags:
            key = parse_precompile_tag(tag)
            if key is not None:
                built.add(key)
    else:
        for tag in existing_tags:
            key = parse_runtime_tag(tag)
            if key is not None:
                built.add(key)

    missing: set[BuildKey] = set()
    for key in desired:
        if key not in built and key not in in_flight:
            missing.add(key)
    return missing


# ---------------------------------------------------------------------------
# compute_retained_flatcar_set
# ---------------------------------------------------------------------------


def compute_retained_flatcar_set(
    current: set[str],
    tracked: set[str],
    history: set[str],
    keep_previous: int,
) -> set[str]:
    """Compute the set of Flatcar versions that should be retained.

    The retained set is:
    - All versions in ``current`` (on-node versions).
    - All versions in ``tracked`` (channel-poller versions).
    - The ``keep_previous`` most recent distinct versions from ``history``
      that are strictly *below* the highest version in ``current``.

    Parameters
    ----------
    current:
        Flatcar versions currently running on cluster nodes.
    tracked:
        Latest release per tracked channel (from poller).
    history:
        Full set of known historical versions (e.g. from existing registry tags).
    keep_previous:
        How many previous versions below the highest current node version to
        retain.
    """
    retained: set[str] = set(current) | set(tracked)

    if keep_previous <= 0 or not current:
        return retained

    # Find the highest version currently on nodes.
    highest_current = max(current, key=_ver)

    # Collect historical versions strictly less than highest_current, sorted
    # descending.
    older = sorted(
        (v for v in history if _ver(v) < _ver(highest_current)),
        key=_ver,
        reverse=True,
    )
    for v in older[:keep_previous]:
        retained.add(v)

    return retained


# ---------------------------------------------------------------------------
# compute_prunable
# ---------------------------------------------------------------------------


def compute_prunable(
    existing_tags: set[str],
    tag_ages: dict[str, "datetime | None"],
    retained_flatcars: set[str],
    min_age: "timedelta",
    *,
    now: "datetime",
    precompile: bool,
) -> set[str]:
    """Return the set of image tags that are eligible for garbage collection.

    A tag is prunable iff ALL of the following hold:
    1. Its Flatcar version is *not* in ``retained_flatcars``.
    2. Its age is known (``tag_ages[tag]`` is not ``None``).
    3. ``now - tag_ages[tag] >= min_age``.

    Parameters
    ----------
    existing_tags:
        All tags currently present in the registry.
    tag_ages:
        Mapping of tag → push timestamp (``None`` means unknown).
    retained_flatcars:
        Set of Flatcar version strings that must be kept.
    min_age:
        Minimum time a tag must have existed before being eligible for pruning.
    now:
        Current timestamp (injected for determinism in tests).
    precompile:
        Selects which tag parser to use.
    """
    parse = parse_precompile_tag if precompile else parse_runtime_tag

    prunable: set[str] = set()
    for tag in existing_tags:
        key = parse(tag)
        if key is None:
            continue
        if key.flatcar in retained_flatcars:
            continue
        age = tag_ages.get(tag)
        if age is None:
            continue
        if now - age >= min_age:
            prunable.add(tag)
    return prunable
