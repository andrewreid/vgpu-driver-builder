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
    """Identifies one (driver, flatcar, kernel?) build combination."""

    driver: str
    flatcar: str
    kernel: str | None = None  # None → runtime mode; set → precompile mode


# ---------------------------------------------------------------------------
# Tag formatters / parsers
# ---------------------------------------------------------------------------

# Runtime tag:   "<driver>-flatcar<flatcar>"
# Precompile tag: "<driver>-<kernel>-flatcar<flatcar>"
#
# The separator between the driver version and the flatcar section is
# "-flatcar", which is a literal string that does not appear in typical
# version numbers.  Kernel is sandwiched between driver and "-flatcar".

_RUNTIME_RE = re.compile(
    r"^(?P<driver>[^-]+(?:-[^f][^-]*)*)-flatcar(?P<flatcar>.+)$"
)

# More precise patterns anchored on the literal "-flatcar" separator.
# Runtime:    <driver>-flatcar<flatcar>
# Precompile: <driver>-<kernel>-flatcar<flatcar>
#
# We need to handle driver versions like "550.54.15" and kernel versions like
# "6.1.119".  Both are dot-separated numerics.  The safe anchor is "-flatcar".


def runtime_tag(key: BuildKey) -> str:
    """Return the image tag for a runtime (non-precompiled) build."""
    return f"{key.driver}-flatcar{key.flatcar}"


def precompile_tag(key: BuildKey) -> str:
    """Return the image tag for a precompiled build."""
    if key.kernel is None:
        raise ValueError("precompile_tag requires key.kernel to be set")
    return f"{key.driver}-{key.kernel}-flatcar{key.flatcar}"


def parse_runtime_tag(tag: str) -> BuildKey | None:
    """Parse a runtime tag back to a ``BuildKey``.

    Returns ``None`` if *tag* does not match the expected format.
    """
    # Find the last occurrence of "-flatcar" as the separator.
    sep = "-flatcar"
    idx = tag.rfind(sep)
    if idx < 1:
        return None
    driver = tag[:idx]
    flatcar = tag[idx + len(sep):]
    if not driver or not flatcar:
        return None
    return BuildKey(driver=driver, flatcar=flatcar, kernel=None)


def parse_precompile_tag(tag: str) -> BuildKey | None:
    """Parse a precompile tag back to a ``BuildKey``.

    Returns ``None`` if *tag* does not match the expected format.
    """
    sep = "-flatcar"
    idx = tag.rfind(sep)
    if idx < 1:
        return None
    flatcar = tag[idx + len(sep):]
    remainder = tag[:idx]  # "<driver>-<kernel>"
    # Split remainder into driver and kernel: driver is everything up to the
    # first "-" followed by a digit (version-like), then the rest is kernel.
    # Because both driver and kernel are version strings (digits and dots with
    # optional leading component), we split on the FIRST "-" that is preceded
    # by a digit and followed by a digit.
    m = re.search(r"(\d)-(\d)", remainder)
    if not m:
        return None
    split_pos = m.start() + 1  # position of the "-"
    driver = remainder[:split_pos]
    kernel = remainder[split_pos + 1:]
    if not driver or not kernel or not flatcar:
        return None
    return BuildKey(driver=driver, flatcar=flatcar, kernel=kernel)


# ---------------------------------------------------------------------------
# compute_desired
# ---------------------------------------------------------------------------


def compute_desired(
    driver_versions: list[str],
    node_pairs: set[tuple[str, str]],
    tracked_pairs: set[tuple[str, str]],
    *,
    precompile: bool,
) -> set[BuildKey]:
    """Compute the complete set of build keys that *should* exist.

    Parameters
    ----------
    driver_versions:
        List of NVIDIA driver version strings from the CRD spec.
    node_pairs:
        Set of ``(flatcar_version, kernel_version)`` tuples observed on nodes.
    tracked_pairs:
        Set of ``(flatcar_version, kernel_version)`` tuples from channel
        poller.
    precompile:
        When ``True`` build precompiled images keyed by kernel; otherwise
        runtime images (kernel is ignored).
    """
    all_pairs: set[tuple[str, str]] = node_pairs | tracked_pairs

    result: set[BuildKey] = set()
    for driver in driver_versions:
        if precompile:
            for flatcar, kernel in all_pairs:
                result.add(BuildKey(driver=driver, flatcar=flatcar, kernel=kernel))
        else:
            # Deduplicate by flatcar version — kernel is irrelevant for runtime.
            seen_flatcar = {fc for fc, _ in all_pairs}
            for flatcar in seen_flatcar:
                result.add(BuildKey(driver=driver, flatcar=flatcar, kernel=None))
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
    parse = parse_precompile_tag if precompile else parse_runtime_tag
    tag_fn = precompile_tag if precompile else runtime_tag

    # Build set of BuildKeys that already have an image.
    built: set[BuildKey] = set()
    for tag in existing_tags:
        key = parse(tag)
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
