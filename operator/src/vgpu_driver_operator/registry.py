"""OCI Distribution v2 registry helpers.

Speaks the OCI Distribution v2 / Docker Registry HTTP API v2 to probe and
prune image tags.  Works with Harbor, ECR, GHCR, GAR, and Docker Hub.

**Transport**: always HTTPS – there is no HTTP fallback in v0.1.0.

**ECR / GAR note**: The AWS SigV4 and GCP service-account JWT token-exchange
flows are out of scope for v0.1.0.  ECR and GAR users must pre-obtain a
short-lived bearer token and pass it via ``RegistryAuth(bearer=...)``.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict
from urllib.parse import urlparse

import requests

REGISTRY_TIMEOUT = (5, 10)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class RegistryAuth(TypedDict, total=False):
    """Parsed from a ``kubernetes.io/dockerconfigjson`` Secret data field.

    Supply either ``username`` + ``password`` (for registries that support
    the Bearer challenge dance), or a pre-obtained ``bearer`` token (ECR,
    GAR, or any registry where you handle token exchange yourself).
    """

    username: str
    password: str
    # A raw bearer token already obtained out-of-band (rare).
    bearer: str


class RegistryError(Exception):
    """Base error for all registry operations."""


class RegistryUnreachable(RegistryError):
    """Raised when the registry cannot be reached due to a network failure.

    Covers DNS resolution failures, TCP connection refused, and read/connect
    timeouts.  Callers that catch ``RegistryError`` continue to work — this
    subclass exists so reconcile can distinguish a transient network outage
    (back off, set a status condition) from a permanent API error.
    """


class TagDeletionDisabled(RegistryError):
    """Raised when the registry returns HTTP 405 for DELETE /manifests/<digest>."""


def _network_error(url: str, exc: requests.RequestException) -> RegistryUnreachable:
    return RegistryUnreachable(f"{url}: {exc}")


# ---------------------------------------------------------------------------
# Internal: token cache & session helpers
# ---------------------------------------------------------------------------

_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)

_OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


class _TokenCache:
    """In-memory bearer-token cache scoped to a single session/request context."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], str] = {}

    def get(self, realm: str, service: str, scope: str) -> str | None:
        return self._cache.get((realm, service, scope))

    def set(self, realm: str, service: str, scope: str, token: str) -> None:
        self._cache[(realm, service, scope)] = token


class _RegistrySession:
    """Thin wrapper around a ``requests.Session`` that handles auth.

    Authentication strategy (per OCI Distribution spec section 7.3):

    1. If ``auth`` contains ``bearer``, send ``Authorization: Bearer <token>``
       on every request without a challenge dance.
    2. Otherwise, send the first request unauthenticated.  On a 401 with a
       ``WWW-Authenticate: Bearer …`` header, fetch a token from the realm
       endpoint and retry once.
    3. If ``auth`` is *None* and we get a 401, raise :class:`RegistryError`.
    """

    def __init__(
        self, auth: RegistryAuth | None, session: requests.Session | None = None
    ) -> None:
        self._auth = auth
        self._session = session or requests.Session()
        # Attach a fresh token cache if the caller did not bring one embedded
        # in the session object (we piggy-back via a custom attribute).
        if not hasattr(self._session, "_registry_token_cache"):
            self._session._registry_token_cache = _TokenCache()  # type: ignore[attr-defined]
        self._token_cache: _TokenCache = self._session._registry_token_cache  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get(self, url: str, **kwargs: object) -> requests.Response:
        """Issue a GET request with registry auth handling."""
        return self._request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: object) -> requests.Response:
        """Issue a HEAD request with registry auth handling."""
        return self._request("HEAD", url, **kwargs)

    def delete(self, url: str, **kwargs: object) -> requests.Response:
        """Issue a DELETE request with registry auth handling."""
        return self._request("DELETE", url, **kwargs)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        """Issue *method* request, handling bearer-token acquisition on 401."""
        auth = self._auth or {}
        kwargs.setdefault("timeout", REGISTRY_TIMEOUT)

        # If a raw bearer is provided, inject it up front and skip the dance.
        if "bearer" in auth:
            headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})  # type: ignore[arg-type]
            headers["Authorization"] = f"Bearer {auth['bearer']}"
            kwargs["headers"] = headers  # type: ignore[assignment]
            try:
                resp = self._session.request(method, url, **kwargs)  # type: ignore[arg-type]
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise _network_error(url, exc) from exc
            except requests.RequestException as exc:
                raise RegistryError(str(exc)) from exc
            return resp

        # --- Unauthenticated first attempt ---
        try:
            resp = self._session.request(method, url, **kwargs)  # type: ignore[arg-type]
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise _network_error(url, exc) from exc
        except requests.RequestException as exc:
            raise RegistryError(str(exc)) from exc

        if resp.status_code != 401:
            return resp

        # --- 401: parse WWW-Authenticate ---
        www_auth = resp.headers.get("WWW-Authenticate", "")
        realm, service, scope = _parse_www_authenticate(www_auth)

        if realm is None:
            raise RegistryError(f"401 with unparseable WWW-Authenticate: {www_auth!r}")

        if not auth:  # auth is None or empty dict
            raise RegistryError("authentication required but no credentials provided")

        # --- Fetch or reuse cached token ---
        token = self._token_cache.get(realm, service or "", scope or "")
        if token is None:
            token = self._fetch_token(realm, service, scope, auth)
            self._token_cache.set(realm, service or "", scope or "", token)

        # --- Retry with bearer token ---
        headers = dict(kwargs.pop("headers", {}) or {})  # type: ignore[arg-type]
        headers["Authorization"] = f"Bearer {token}"
        kwargs["headers"] = headers  # type: ignore[assignment]
        try:
            resp2 = self._session.request(method, url, **kwargs)  # type: ignore[arg-type]
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise _network_error(url, exc) from exc
        except requests.RequestException as exc:
            raise RegistryError(str(exc)) from exc

        if resp2.status_code == 401:
            raise RegistryError("registry authentication failed (token rejected on retry)")

        return resp2

    @staticmethod
    def _fetch_token(
        realm: str,
        service: str | None,
        scope: str | None,
        auth: RegistryAuth,
    ) -> str:
        """Retrieve a bearer token from *realm* using Basic auth credentials."""
        params: dict[str, str] = {}
        if service:
            params["service"] = service
        if scope:
            params["scope"] = scope

        username = auth.get("username", "")
        password = auth.get("password", "")

        try:
            resp = requests.get(
                realm,
                params=params,
                auth=(username, password),
                timeout=REGISTRY_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise _network_error(realm, exc) from exc
        except requests.RequestException as exc:
            raise RegistryError(f"token fetch failed: {exc}") from exc

        if resp.status_code == 401:
            raise RegistryError("registry token endpoint rejected credentials (401)")

        if not resp.ok:
            raise RegistryError(
                f"token fetch failed: HTTP {resp.status_code} from {realm}"
            )

        try:
            data = resp.json()
            return data["token"]
        except (ValueError, KeyError) as exc:
            raise RegistryError(f"malformed token response from {realm}: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal: WWW-Authenticate parsing
# ---------------------------------------------------------------------------


def _parse_www_authenticate(header: str) -> tuple[str | None, str | None, str | None]:
    """Parse ``Bearer realm="...",service="...",scope="..."`` header.

    Returns *(realm, service, scope)*.  Any missing field is *None*.
    Returns *(None, None, None)* if header does not start with ``Bearer``.
    """
    if not header.strip().startswith("Bearer"):
        return None, None, None

    def _extract(key: str) -> str | None:
        m = re.search(rf'{key}="([^"]*)"', header)
        return m.group(1) if m else None

    return _extract("realm"), _extract("service"), _extract("scope")


# ---------------------------------------------------------------------------
# Internal: repository URL helpers
# ---------------------------------------------------------------------------


def _split_repository(repository: str) -> tuple[str, str]:
    """Split ``host/path`` into *(host, path)*."""
    idx = repository.find("/")
    if idx == -1:
        raise RegistryError(f"invalid repository format (no '/') in {repository!r}")
    return repository[:idx], repository[idx + 1:]


def _manifests_url(host: str, path: str, ref: str) -> str:
    return f"https://{host}/v2/{path}/manifests/{ref}"


def _blobs_url(host: str, path: str, digest: str) -> str:
    return f"https://{host}/v2/{path}/blobs/{digest}"


def _tags_url(host: str, path: str) -> str:
    return f"https://{host}/v2/{path}/tags/list"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_dockerconfigjson(blob: bytes | str, registry_host: str) -> RegistryAuth:
    """Parse ``.dockerconfigjson`` bytes and return :class:`RegistryAuth` for *registry_host*.

    *blob* must be the already-base64-decoded bytes (or str) of the Secret
    data field — the caller is responsible for that step.

    Raises :class:`RegistryError` if no matching entry is found.
    """
    if isinstance(blob, bytes):
        blob = blob.decode()

    try:
        cfg = json.loads(blob)
    except ValueError as exc:
        raise RegistryError(f"invalid dockerconfigjson: {exc}") from exc

    if not isinstance(cfg, dict) or not isinstance(cfg.get("auths", {}), dict):
        raise RegistryError("invalid dockerconfigjson auths object")
    auths: dict[str, dict[str, str]] = cfg.get("auths", {})

    def _normalise(key: str) -> str:
        if key.startswith("https://") or key.startswith("http://"):
            return urlparse(key).netloc or key
        return key

    # Try exact match first, then normalised match.
    entry: dict[str, str] | None = auths.get(registry_host)
    if entry is None:
        norm_host = _normalise(registry_host)
        for k, v in auths.items():
            if _normalise(k) == norm_host:
                entry = v
                break

    if entry is None:
        raise RegistryError(
            f"no dockerconfigjson entry for registry {registry_host!r}"
        )
    if not isinstance(entry, dict):
        raise RegistryError("invalid dockerconfigjson credential entry")

    if "auth" in entry:
        try:
            decoded = base64.b64decode(entry["auth"], validate=True).decode()
            username, separator, password = decoded.partition(":")
            if not separator or not username or not password:
                raise ValueError("expected nonempty username:password")
        except Exception as exc:
            raise RegistryError(f"cannot decode auth field: {exc}") from exc
        return RegistryAuth(username=username, password=password)

    if all(isinstance(entry.get(key), str) and entry[key] for key in ("username", "password")):
        return RegistryAuth(username=entry["username"], password=entry["password"])

    raise RegistryError(
        f"dockerconfigjson entry for {registry_host!r} has no usable credentials"
    )


def find_matching_tags(
    repository: str,
    auth: RegistryAuth | None,
    prefix: str,
    suffix: str,
    *,
    session: requests.Session | None = None,
) -> set[str]:
    """Return tags in *repository* that start with *prefix* and end with *suffix*.

    Used for precompile idempotency: finds tags of the form
    ``<driver>-<kernel>-flatcar<flatcar>`` without knowing the kernel in advance.
    Returns an empty set if no tags match or the repository does not exist.
    """
    tags = list_tags(repository, auth, session=session)
    return {
        t for t in tags
        if t.startswith(prefix) and t.endswith(suffix) and len(t) > len(prefix) + len(suffix)
    }


def list_tags(
    repository: str,
    auth: RegistryAuth | None,
    *,
    session: requests.Session | None = None,
) -> set[str]:
    """Return the full set of tags for *repository*.

    Follows OCI ``Link: <...>; rel="next"`` pagination.
    A 404 response (empty or non-existent repo) returns an empty set rather
    than raising an error.
    """
    host, path = _split_repository(repository)
    reg = _RegistrySession(auth, session)

    url: str | None = _tags_url(host, path) + "?n=1000"
    tags: set[str] = set()
    visited: set[str] = set()

    while url is not None:
        if url in visited or urlparse(url).netloc != host or urlparse(url).scheme != "https":
            raise RegistryError("list_tags: unsafe or repeated pagination link")
        visited.add(url)
        resp = reg.get(url)

        if resp.status_code == 404:
            if len(visited) > 1:
                raise RegistryError("list_tags: pagination disappeared")
            return set()

        if not resp.ok:
            raise RegistryError(
                f"list_tags: unexpected HTTP {resp.status_code} from {url}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise RegistryError(f"list_tags: non-JSON response: {exc}") from exc

        if not isinstance(data, dict) or "tags" not in data:
            raise RegistryError("list_tags: missing tags in response")
        page_tags = data.get("tags")
        if page_tags is None:
            page_tags = []
        if not isinstance(page_tags, list) or any(not isinstance(t, str) for t in page_tags):
            raise RegistryError("list_tags: invalid tags in response")
        tags.update(page_tags)

        link = resp.headers.get("Link", "")
        url = _parse_link_next(link, host)
        if link and url is None:
            raise RegistryError("list_tags: unrecognised pagination link")

    return tags


def tag_created_at(
    repository: str,
    tag: str,
    auth: RegistryAuth | None,
    *,
    session: requests.Session | None = None,
) -> datetime | None:
    """Return the ``created`` timestamp for *tag*, or *None* on content errors.

    Fetches the manifest, follows a manifest-list/OCI-index to the first
    child manifest, then fetches the config blob and parses its ``created``
    field (RFC3339).  Returns *None* on manifest 404, missing field, or any
    parse error. Raises :class:`RegistryUnreachable` for network failures.
    """
    host, path = _split_repository(repository)
    reg = _RegistrySession(auth, session)

    manifest_url = _manifests_url(host, path, tag)
    try:
        resp = reg.get(manifest_url, headers={"Accept": _MANIFEST_ACCEPT})
    except RegistryUnreachable:
        raise
    except RegistryError:
        return None

    if resp.status_code == 404:
        return None
    if not resp.ok:
        return None

    try:
        manifest = resp.json()
    except ValueError:
        return None

    # Determine media type from JSON body first, fall back to Content-Type.
    media_type = manifest.get("mediaType") or resp.headers.get("Content-Type", "")
    # Strip content-type parameters (e.g. "; charset=utf-8")
    media_type = media_type.split(";")[0].strip()

    if media_type in _OCI_INDEX_MEDIA_TYPES:
        manifests = manifest.get("manifests", [])
        if not manifests:
            return None
        child_digest = manifests[0].get("digest")
        if not child_digest:
            return None
        child_url = _manifests_url(host, path, child_digest)
        try:
            child_resp = reg.get(child_url, headers={"Accept": _MANIFEST_ACCEPT})
        except RegistryUnreachable:
            raise
        except RegistryError:
            return None
        if not child_resp.ok:
            return None
        try:
            manifest = child_resp.json()
        except ValueError:
            return None

    config = manifest.get("config", {})
    config_digest = config.get("digest")
    if not config_digest:
        return None

    blob_url = _blobs_url(host, path, config_digest)
    try:
        blob_resp = reg.get(blob_url)
    except RegistryUnreachable:
        raise
    except RegistryError:
        return None
    if not blob_resp.ok:
        return None

    try:
        config_data = blob_resp.json()
    except ValueError:
        return None

    created_str = config_data.get("created")
    if not created_str:
        return None

    try:
        return datetime.fromisoformat(created_str.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class ManifestInfo:
    digest: str
    children: tuple[str, ...]
    created_at: datetime | None


@dataclass(frozen=True)
class RepositoryInventory:
    tags: dict[str, str]
    manifests: dict[str, ManifestInfo]


def inventory_repository(
    repository: str,
    auth: RegistryAuth | None,
    *,
    session: requests.Session | None = None,
) -> RepositoryInventory:
    """Read every tagged manifest and its reference closure, or fail closed.

    Content without a reliable image creation time is protected. Transport,
    missing-reference and structural errors invalidate the entire inventory.
    """
    host, path = _split_repository(repository)
    reg = _RegistrySession(auth, session)
    manifests: dict[str, ManifestInfo] = {}
    visiting: set[str] = set()

    def get_json(url: str) -> tuple[dict, requests.Response]:
        response = reg.get(url, headers={"Accept": _MANIFEST_ACCEPT})
        if not response.ok:
            raise RegistryError(f"inventory: HTTP {response.status_code} from {url}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RegistryError(f"inventory: invalid JSON from {url}") from exc
        if not isinstance(body, dict):
            raise RegistryError(f"inventory: expected object from {url}")
        return body, response

    def inspect(ref: str) -> ManifestInfo:
        if ref in manifests:
            return manifests[ref]
        body, response = get_json(_manifests_url(host, path, ref))
        digest = response.headers.get("Docker-Content-Digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RegistryError("inventory: missing or unsupported manifest digest")
        if ref.startswith("sha256:") and ref != digest:
            raise RegistryError("inventory: manifest digest mismatch")
        if digest in visiting:
            raise RegistryError("inventory: cyclic manifest references")
        if digest in manifests:
            return manifests[digest]
        visiting.add(digest)
        media_type = (body.get("mediaType") or response.headers.get("Content-Type", ""))
        media_type = media_type.split(";")[0].strip()
        children: list[str] = []
        created: datetime | None = None

        def child(descriptor: dict) -> ManifestInfo:
            if not isinstance(descriptor, dict) or not isinstance(descriptor.get("digest"), str):
                raise RegistryError("inventory: invalid manifest descriptor")
            child_ref = descriptor["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", child_ref):
                raise RegistryError("inventory: invalid child digest")
            info = inspect(child_ref)
            children.append(info.digest)
            return info

        if media_type in _OCI_INDEX_MEDIA_TYPES:
            descriptors = body.get("manifests")
            if not isinstance(descriptors, list):
                raise RegistryError("inventory: invalid index")
            ages: list[datetime | None] = []
            for descriptor in descriptors:
                info = child(descriptor)
                annotations = descriptor.get("annotations") or {}
                if annotations.get("vnd.docker.reference.type") != "attestation-manifest":
                    ages.append(info.created_at)
            if ages and all(age is not None for age in ages):
                created = max(age for age in ages if age is not None)
        elif media_type in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }:
            config = body.get("config")
            if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
                raise RegistryError("inventory: missing config descriptor")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", config["digest"]):
                raise RegistryError("inventory: invalid config digest")
            config_body, _ = get_json(_blobs_url(host, path, config["digest"]))
            try:
                value = config_body.get("created")
                created = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if created.tzinfo is None or created.utcoffset() is None:
                    created = None
            except (AttributeError, ValueError, TypeError):
                created = None
            if body.get("artifactType"):
                created = None
        else:
            # Unknown structures could conceal references to otherwise eligible images.
            raise RegistryError(f"inventory: unsupported manifest media type {media_type!r}")
        if body.get("subject") is not None:
            child(body["subject"])
        visiting.remove(digest)
        info = ManifestInfo(digest, tuple(sorted(set(children))), created)
        manifests[digest] = info
        return info

    tags = {tag: inspect(tag).digest for tag in sorted(list_tags(repository, auth, session=session))}
    return RepositoryInventory(tags, manifests)


def delete_manifest(
    repository: str,
    digest: str,
    auth: RegistryAuth | None,
    *,
    session: requests.Session | None = None,
) -> None:
    """Delete exactly the digest checked by retention, without resolving a tag."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RegistryError("delete_manifest: invalid digest")
    host, path = _split_repository(repository)
    response = _RegistrySession(auth, session).delete(_manifests_url(host, path, digest))
    if response.status_code == 405:
        raise TagDeletionDisabled(f"manifest deletion disabled for {repository}")
    if response.status_code != 202:
        raise RegistryError(f"delete_manifest: unexpected HTTP {response.status_code}")


def delete_tag(
    repository: str,
    tag: str,
    auth: RegistryAuth | None,
    *,
    session: requests.Session | None = None,
) -> None:
    """Delete *tag* from *repository* via OCI manifest delete.

    Resolves the tag to a content-addressable digest using
    ``HEAD /manifests/{tag}`` (reading the ``Docker-Content-Digest`` header),
    then issues ``DELETE /manifests/{digest}``.

    Raises :class:`TagDeletionDisabled` on HTTP 405.
    Raises :class:`RegistryError` on HEAD failure or other unexpected errors.
    """
    host, path = _split_repository(repository)
    reg = _RegistrySession(auth, session)

    head_url = _manifests_url(host, path, tag)
    head_resp = reg.head(head_url, headers={"Accept": _MANIFEST_ACCEPT})

    if not head_resp.ok:
        raise RegistryError(
            f"delete_tag: HEAD {head_url} returned HTTP {head_resp.status_code}"
        )

    digest = head_resp.headers.get("Docker-Content-Digest")
    if not digest:
        raise RegistryError(
            "delete_tag: HEAD response missing Docker-Content-Digest header"
        )

    delete_url = _manifests_url(host, path, digest)
    del_resp = reg.delete(delete_url)

    if del_resp.status_code == 405:
        raise TagDeletionDisabled(
            f"registry does not support tag deletion (405) for {repository}:{tag}"
        )

    if not del_resp.ok:
        raise RegistryError(
            f"delete_tag: DELETE {delete_url} returned HTTP {del_resp.status_code}"
        )


# ---------------------------------------------------------------------------
# Internal: Link header parser
# ---------------------------------------------------------------------------


def _parse_link_next(link_header: str, host: str) -> str | None:
    """Extract the ``rel="next"`` URL from an RFC 5988 Link header.

    Returns the absolute URL string, or *None* if absent.
    Relative paths are resolved against ``https://{host}``.
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        m = re.match(r'<([^>]+)>;\s*rel="next"', part)
        if m:
            url = m.group(1)
            if url.startswith("/"):
                url = f"https://{host}{url}"
            return url
    return None
