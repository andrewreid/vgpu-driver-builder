"""Flatcar Linux release metadata helpers."""

from __future__ import annotations

import re

import requests as _requests


class FlatcarFeedError(RuntimeError):
    """Raised when a Flatcar release feed request fails or returns unexpected data."""


# Flatcar osImage string looks like:
#   "Flatcar Container Linux by Kinvolk 4230.2.3 (Oklo)"
_OS_IMAGE_RE = re.compile(
    r"Flatcar Container Linux[^0-9]*(\d+\.\d+\.\d+)",
    re.IGNORECASE,
)

_NFD_OS_VERSION = "feature.node.kubernetes.io/system-os_release.VERSION_ID"

# NFD sets VERSION_ID to e.g. "3815.2.0" on Flatcar — the ID label is set
# only on Flatcar nodes; validate it looks like a Flatcar version triple.
_FLATCAR_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def parse_flatcar_version_from_os_image(os_image: str) -> str | None:
    """Extract Flatcar version string from a node osImage field.

    Returns the version string (e.g. ``"4230.2.3"``) or ``None`` for
    non-Flatcar OS strings.  The string must explicitly contain "Flatcar"
    to be accepted — plain version numbers from other distros are rejected.
    """
    if not os_image:
        return None
    m = _OS_IMAGE_RE.search(os_image)
    return m.group(1) if m else None


def flatcar_version_from_node(node: dict) -> str | None:
    """Return the Flatcar version for a Kubernetes node dict.

    Preference order:
    1. NFD label ``feature.node.kubernetes.io/system-os_release.VERSION_ID``
       (only accepted when it matches the ``X.Y.Z`` Flatcar version pattern
       AND the osImage also contains "Flatcar", or when no osImage is present
       but the label looks like a valid triple — conservative: we also check
       the osImage does not indicate a different OS).
    2. Parse ``node.status.nodeInfo.osImage``

    Returns ``None`` if the node is not running Flatcar.
    """
    labels: dict = (node.get("metadata") or {}).get("labels") or {}
    os_image: str = (
        (node.get("status") or {}).get("nodeInfo") or {}
    ).get("osImage", "")

    # Only trust the NFD VERSION_ID label when the osImage also confirms this
    # is a Flatcar node (or osImage is absent).  This prevents a Debian node
    # whose VERSION_ID="12" from being mistaken for Flatcar version "12".
    nfd_val = labels.get(_NFD_OS_VERSION)
    if nfd_val and _FLATCAR_VERSION_RE.match(nfd_val):
        # Confirm via osImage: either no osImage, or osImage contains "Flatcar".
        if not os_image or "flatcar" in os_image.lower():
            return nfd_val

    return parse_flatcar_version_from_os_image(os_image)


def latest_release(
    channel: str,
    arch: str = "amd64",
    *,
    session=None,
) -> str:
    """Return the latest Flatcar version string for *channel*/*arch*.

    Fetches ``https://{channel}.release.flatcar-linux.net/{arch}-usr/current/version.txt``
    and parses the ``FLATCAR_VERSION=`` line.

    Parameters
    ----------
    channel:
        Release channel, e.g. ``"stable"``, ``"lts"``, ``"beta"``, ``"alpha"``.
    arch:
        CPU architecture string used in the URL path (default ``"amd64"``).
    session:
        Optional ``requests.Session``-compatible object for dependency
        injection in tests.  When ``None`` a plain ``requests.get`` call
        is used.

    Raises
    ------
    FlatcarFeedError
        On HTTP error or if the expected line is not found in the response.
    """
    url = f"https://{channel}.release.flatcar-linux.net/{arch}-usr/current/version.txt"
    return _fetch_version_key(url, "FLATCAR_VERSION", session=session)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_version_key(url: str, key: str, *, session=None) -> str:
    """GET *url* and return the value of ``KEY=value`` line.

    Raises ``FlatcarFeedError`` on any HTTP error or missing key.
    """
    try:
        if session is not None:
            resp = session.get(url, timeout=15)
        else:
            resp = _requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        raise FlatcarFeedError(f"Failed to fetch {url}: {exc}") from exc

    text: str = resp.text
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    raise FlatcarFeedError(
        f"Key {key!r} not found in response from {url}"
    )
