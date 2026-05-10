"""Tests for vgpu_driver_operator.registry (OCI Distribution v2 client)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
import requests
import responses as rsps_lib
from responses import matchers

from vgpu_driver_operator.registry import (
    REGISTRY_TIMEOUT,
    RegistryAuth,
    RegistryError,
    RegistryUnreachable,
    TagDeletionDisabled,
    delete_tag,
    list_tags,
    parse_dockerconfigjson,
    tag_created_at,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = "registry.example.com/gpu-drivers/vgpu"
HOST = "registry.example.com"
PATH = "gpu-drivers/vgpu"


def _auth_header(username: str, password: str) -> str:
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {creds}"


def _make_dockerconfigjson(auths: dict) -> bytes:
    return json.dumps({"auths": auths}).encode()


BEARER_CHALLENGE = (
    'Bearer realm="https://auth.example.com/token",'
    'service="registry.example.com",'
    'scope="repository:gpu-drivers/vgpu:pull"'
)


# ---------------------------------------------------------------------------
# 1. parse_dockerconfigjson
# ---------------------------------------------------------------------------


class TestParseDockerconfigjson:
    def test_exact_host_match_auth_field(self):
        creds = base64.b64encode(b"user:secret").decode()
        blob = _make_dockerconfigjson({HOST: {"auth": creds}})
        result = parse_dockerconfigjson(blob, HOST)
        assert result["username"] == "user"
        assert result["password"] == "secret"

    def test_host_match_with_https_prefix(self):
        creds = base64.b64encode(b"user2:pass2").decode()
        blob = _make_dockerconfigjson({f"https://{HOST}": {"auth": creds}})
        result = parse_dockerconfigjson(blob, HOST)
        assert result["username"] == "user2"
        assert result["password"] == "pass2"

    def test_username_password_direct_fields(self):
        blob = _make_dockerconfigjson({HOST: {"username": "u", "password": "p"}})
        result = parse_dockerconfigjson(blob, HOST)
        assert result["username"] == "u"
        assert result["password"] == "p"

    def test_no_match_raises(self):
        blob = _make_dockerconfigjson({"other.registry.io": {"username": "x", "password": "y"}})
        with pytest.raises(RegistryError, match="no dockerconfigjson entry"):
            parse_dockerconfigjson(blob, HOST)

    def test_bytes_input_accepted(self):
        creds = base64.b64encode(b"a:b").decode()
        blob = _make_dockerconfigjson({HOST: {"auth": creds}})
        result = parse_dockerconfigjson(blob, HOST)
        assert result["username"] == "a"

    def test_http_prefix_stripped(self):
        creds = base64.b64encode(b"x:y").decode()
        blob = _make_dockerconfigjson({f"http://{HOST}": {"auth": creds}})
        result = parse_dockerconfigjson(blob, HOST)
        assert result["username"] == "x"


# ---------------------------------------------------------------------------
# 2. list_tags
# ---------------------------------------------------------------------------


class TestListTags:
    @rsps_lib.activate
    def test_happy_path_single_page(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json={"tags": ["a", "b", "c"]},
            status=200,
            match=[matchers.request_kwargs_matcher({"timeout": REGISTRY_TIMEOUT})],
        )
        result = list_tags(REPO, None)
        assert result == {"a", "b", "c"}

    @rsps_lib.activate
    def test_timeout_raises_registry_unreachable(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            body=requests.Timeout("connect timed out"),
            match=[matchers.request_kwargs_matcher({"timeout": REGISTRY_TIMEOUT})],
        )

        with pytest.raises(RegistryUnreachable, match=HOST):
            list_tags(REPO, None)

    @rsps_lib.activate
    def test_paginated(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json={"tags": ["a", "b"]},
            status=200,
            headers={
                "Link": f'</v2/{PATH}/tags/list?n=1000&last=b>; rel="next"'
            },
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json={"tags": ["c", "d"]},
            status=200,
        )
        result = list_tags(REPO, None)
        assert result == {"a", "b", "c", "d"}

    @rsps_lib.activate
    def test_404_returns_empty_set(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json={"errors": [{"code": "NAME_UNKNOWN"}]},
            status=404,
        )
        result = list_tags(REPO, None)
        assert result == set()

    @rsps_lib.activate
    def test_bearer_challenge_flow(self):
        """401 → fetch token from realm → retry succeeds."""
        # First call: 401 with challenge
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        # Token endpoint
        rsps_lib.add(
            rsps_lib.GET,
            "https://auth.example.com/token",
            json={"token": "mytoken123"},
            status=200,
        )
        # Retry with bearer
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json={"tags": ["v1"]},
            status=200,
            match=[matchers.header_matcher({"Authorization": "Bearer mytoken123"})],
        )
        auth = RegistryAuth(username="user", password="pass")
        result = list_tags(REPO, auth)
        assert result == {"v1"}

    @rsps_lib.activate
    def test_bearer_challenge_token_401_raises(self):
        """Token endpoint returns 401 → RegistryError."""
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        rsps_lib.add(
            rsps_lib.GET,
            "https://auth.example.com/token",
            status=401,
        )
        auth = RegistryAuth(username="bad", password="creds")
        with pytest.raises(RegistryError, match="rejected credentials"):
            list_tags(REPO, auth)

    @rsps_lib.activate
    def test_bearer_challenge_token_timeout_raises_unreachable(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
            match=[matchers.request_kwargs_matcher({"timeout": REGISTRY_TIMEOUT})],
        )
        rsps_lib.add(
            rsps_lib.GET,
            "https://auth.example.com/token",
            body=requests.Timeout("read timed out"),
            match=[matchers.request_kwargs_matcher({"timeout": REGISTRY_TIMEOUT})],
        )

        auth = RegistryAuth(username="bad", password="creds")
        with pytest.raises(RegistryUnreachable, match="auth.example.com"):
            list_tags(REPO, auth)

    @rsps_lib.activate
    def test_no_auth_401_raises(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        with pytest.raises(RegistryError, match="authentication required"):
            list_tags(REPO, None)


# ---------------------------------------------------------------------------
# 3. tag_created_at
# ---------------------------------------------------------------------------

_MANIFEST_V2 = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
    "config": {
        "mediaType": "application/vnd.docker.container.image.v1+json",
        "digest": "sha256:configabc",
        "size": 1234,
    },
    "layers": [],
}

_CONFIG_BLOB = {"created": "2024-03-15T12:00:00Z", "architecture": "amd64"}

_OCI_INDEX = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:childmanifest",
            "size": 500,
            "platform": {"os": "linux", "architecture": "amd64"},
        }
    ],
}

_CHILD_MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": "sha256:childconfig",
        "size": 400,
    },
    "layers": [],
}


class TestTagCreatedAt:
    @rsps_lib.activate
    def test_manifest_to_config_returns_datetime(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            json=_MANIFEST_V2,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/blobs/sha256:configabc",
            json=_CONFIG_BLOB,
            status=200,
        )
        result = tag_created_at(REPO, "v1", None)
        assert result == datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)

    @rsps_lib.activate
    def test_manifest_404_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/missing",
            json={},
            status=404,
        )
        result = tag_created_at(REPO, "missing", None)
        assert result is None

    @rsps_lib.activate
    def test_manifest_timeout_raises_registry_unreachable(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            body=requests.Timeout("read timed out"),
            match=[matchers.request_kwargs_matcher({"timeout": REGISTRY_TIMEOUT})],
        )

        with pytest.raises(RegistryUnreachable, match=HOST):
            tag_created_at(REPO, "v1", None)

    @rsps_lib.activate
    def test_oci_index_picks_first_child(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/latest",
            json=_OCI_INDEX,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/sha256:childmanifest",
            json=_CHILD_MANIFEST,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/blobs/sha256:childconfig",
            json={"created": "2024-06-01T08:30:00Z"},
            status=200,
        )
        result = tag_created_at(REPO, "latest", None)
        assert result == datetime(2024, 6, 1, 8, 30, 0, tzinfo=timezone.utc)

    @rsps_lib.activate
    def test_config_no_created_field_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v2",
            json=_MANIFEST_V2,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/blobs/sha256:configabc",
            json={"architecture": "amd64"},  # no "created"
            status=200,
        )
        result = tag_created_at(REPO, "v2", None)
        assert result is None

    @rsps_lib.activate
    def test_malformed_config_json_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v3",
            json=_MANIFEST_V2,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/blobs/sha256:configabc",
            body=b"not-json{{{",
            status=200,
        )
        result = tag_created_at(REPO, "v3", None)
        assert result is None


# ---------------------------------------------------------------------------
# 4. delete_tag
# ---------------------------------------------------------------------------

DIGEST = "sha256:deadbeef"


class TestDeleteTag:
    @rsps_lib.activate
    def test_head_then_delete_202(self):
        rsps_lib.add(
            rsps_lib.HEAD,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            status=200,
            headers={"Docker-Content-Digest": DIGEST},
        )
        rsps_lib.add(
            rsps_lib.DELETE,
            f"https://{HOST}/v2/{PATH}/manifests/{DIGEST}",
            status=202,
        )
        delete_tag(REPO, "v1", None)  # should not raise

    @rsps_lib.activate
    def test_head_timeout_raises_registry_unreachable(self):
        rsps_lib.add(
            rsps_lib.HEAD,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            body=requests.ConnectionError("connection refused"),
            match=[matchers.request_kwargs_matcher({"timeout": REGISTRY_TIMEOUT})],
        )

        with pytest.raises(RegistryUnreachable, match=HOST):
            delete_tag(REPO, "v1", None)

    @rsps_lib.activate
    def test_delete_405_raises_tag_deletion_disabled(self):
        rsps_lib.add(
            rsps_lib.HEAD,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            status=200,
            headers={"Docker-Content-Digest": DIGEST},
        )
        rsps_lib.add(
            rsps_lib.DELETE,
            f"https://{HOST}/v2/{PATH}/manifests/{DIGEST}",
            status=405,
        )
        with pytest.raises(TagDeletionDisabled):
            delete_tag(REPO, "v1", None)

    @rsps_lib.activate
    def test_head_404_raises_registry_error(self):
        rsps_lib.add(
            rsps_lib.HEAD,
            f"https://{HOST}/v2/{PATH}/manifests/missing",
            status=404,
        )
        with pytest.raises(RegistryError, match="HEAD.*404"):
            delete_tag(REPO, "missing", None)


# ---------------------------------------------------------------------------
# 5. Registry-specific auth fixtures
# ---------------------------------------------------------------------------

_TAGS_RESPONSE = {"tags": ["latest"]}

# Shared bearer challenge used by all challenge-flow tests
def _challenge(realm: str, host: str, path: str) -> str:
    return (
        f'Bearer realm="{realm}",'
        f'service="{host}",'
        f'scope="repository:{path}:pull"'
    )


class TestRegistryAuthFixtures:
    """Verify that the correct Authorization header is built for each registry type."""

    @rsps_lib.activate
    def test_harbor_basic_then_bearer(self):
        """Harbor: username + password → Bearer challenge flow."""
        realm = "https://harbor.example.com/service/token"
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": _challenge(realm, HOST, PATH)},
        )
        rsps_lib.add(
            rsps_lib.GET,
            realm,
            json={"token": "harbor-token-xyz"},
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json=_TAGS_RESPONSE,
            status=200,
            match=[matchers.header_matcher({"Authorization": "Bearer harbor-token-xyz"})],
        )
        auth = RegistryAuth(username="robot$project", password="harbor-secret")
        result = list_tags(REPO, auth)
        assert result == {"latest"}

    @rsps_lib.activate
    def test_ecr_aws_bearer_token(self):
        """ECR: pre-obtained bearer passed directly, no challenge needed."""
        ecr_token = "AWS:eyJhbGciOiJSUz..."
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json=_TAGS_RESPONSE,
            status=200,
            match=[matchers.header_matcher({"Authorization": f"Bearer {ecr_token}"})],
        )
        auth = RegistryAuth(bearer=ecr_token)
        result = list_tags(REPO, auth)
        assert result == {"latest"}

    @rsps_lib.activate
    def test_ghcr_bearer_token(self):
        """GHCR: GitHub personal access token passed as bearer."""
        ghcr_token = "ghp_someGitHubToken123"
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json=_TAGS_RESPONSE,
            status=200,
            match=[matchers.header_matcher({"Authorization": f"Bearer {ghcr_token}"})],
        )
        auth = RegistryAuth(bearer=ghcr_token)
        result = list_tags(REPO, auth)
        assert result == {"latest"}

    @rsps_lib.activate
    def test_gar_json_key_basic_then_bearer(self):
        """GAR: username=_json_key_base64, password=<JSON key> → bearer challenge."""
        realm = "https://oauth2.googleapis.com/token"
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": _challenge(realm, HOST, PATH)},
        )
        rsps_lib.add(
            rsps_lib.GET,
            realm,
            json={"token": "ya29.gar-token"},
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json=_TAGS_RESPONSE,
            status=200,
            match=[matchers.header_matcher({"Authorization": "Bearer ya29.gar-token"})],
        )
        gar_json_key = json.dumps({"type": "service_account", "project_id": "my-project"})
        auth = RegistryAuth(username="_json_key_base64", password=gar_json_key)
        result = list_tags(REPO, auth)
        assert result == {"latest"}

    @rsps_lib.activate
    def test_dockerhub_bearer_challenge(self):
        """Docker Hub: challenge at auth.docker.io."""
        realm = "https://auth.docker.io/token"
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": _challenge(realm, HOST, PATH)},
        )
        rsps_lib.add(
            rsps_lib.GET,
            realm,
            json={"token": "hub-token-abc"},
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json=_TAGS_RESPONSE,
            status=200,
            match=[matchers.header_matcher({"Authorization": "Bearer hub-token-abc"})],
        )
        auth = RegistryAuth(username="dockeruser", password="dockerpass")
        result = list_tags(REPO, auth)
        assert result == {"latest"}


# ---------------------------------------------------------------------------
# 6. Additional edge-case / coverage tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    # --- parse_dockerconfigjson edge cases ---

    def test_invalid_json_raises(self):
        with pytest.raises(RegistryError, match="invalid dockerconfigjson"):
            parse_dockerconfigjson(b"not-json", HOST)

    def test_entry_with_no_usable_credentials_raises(self):
        blob = _make_dockerconfigjson({HOST: {"email": "user@example.com"}})
        with pytest.raises(RegistryError, match="no usable credentials"):
            parse_dockerconfigjson(blob, HOST)

    # --- list_tags edge cases ---

    @rsps_lib.activate
    def test_list_tags_non_200_raises(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=500,
            json={"message": "internal error"},
        )
        with pytest.raises(RegistryError, match="500"):
            list_tags(REPO, None)

    @rsps_lib.activate
    def test_list_tags_non_json_raises(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=200,
            body=b"not-json{{",
        )
        with pytest.raises(RegistryError, match="non-JSON"):
            list_tags(REPO, None)

    @rsps_lib.activate
    def test_list_tags_401_no_bearer_in_www_auth(self):
        """401 with Basic challenge (no Bearer) → RegistryError."""
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Registry"'},
        )
        with pytest.raises(RegistryError, match="unparseable WWW-Authenticate"):
            list_tags(REPO, RegistryAuth(username="u", password="p"))

    @rsps_lib.activate
    def test_list_tags_token_endpoint_non_401_error(self):
        """Token endpoint returns 500 → RegistryError."""
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        rsps_lib.add(
            rsps_lib.GET,
            "https://auth.example.com/token",
            status=503,
        )
        with pytest.raises(RegistryError, match="token fetch failed"):
            list_tags(REPO, RegistryAuth(username="u", password="p"))

    @rsps_lib.activate
    def test_list_tags_token_missing_key(self):
        """Token endpoint returns JSON without 'token' key → RegistryError."""
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        rsps_lib.add(
            rsps_lib.GET,
            "https://auth.example.com/token",
            json={"access_token": "oops"},  # wrong key
            status=200,
        )
        with pytest.raises(RegistryError, match="malformed token response"):
            list_tags(REPO, RegistryAuth(username="u", password="p"))

    @rsps_lib.activate
    def test_list_tags_retry_still_401(self):
        """After token fetch, second request returns 401 → RegistryError."""
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        rsps_lib.add(
            rsps_lib.GET,
            "https://auth.example.com/token",
            json={"token": "bad-token"},
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        with pytest.raises(RegistryError, match="token rejected on retry"):
            list_tags(REPO, RegistryAuth(username="u", password="p"))

    @rsps_lib.activate
    def test_list_tags_paginated_token_fetched_once(self):
        """Paginated list: second page re-uses cached token — realm hit only once."""
        # First page: 401 then token fetch then success
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            status=401,
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        rsps_lib.add(
            rsps_lib.GET,
            "https://auth.example.com/token",
            json={"token": "cached-token"},
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json={"tags": ["a"]},
            status=200,
            headers={"Link": f'</v2/{PATH}/tags/list?last=a>; rel="next"'},
            match=[matchers.header_matcher({"Authorization": "Bearer cached-token"})],
        )
        # Second page: registry returns 200 directly (no second challenge).
        # The session's _RegistrySession is instantiated fresh here so this
        # tests that the _TokenCache on the shared session is reused.
        # Actually list_tags creates one _RegistrySession per call; so we
        # test within-call pagination reuse: the second page GET goes out
        # unauthenticated (no header match) and the registry just responds.
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/tags/list",
            json={"tags": ["b"]},
            status=200,
        )
        auth = RegistryAuth(username="u", password="p")
        result = list_tags(REPO, auth)
        assert result == {"a", "b"}
        # Token endpoint was called exactly once
        token_calls = [c for c in rsps_lib.calls if "auth.example.com" in c.request.url]
        assert len(token_calls) == 1

    # --- tag_created_at edge cases ---

    @rsps_lib.activate
    def test_tag_created_at_non_ok_manifest_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            status=500,
        )
        assert tag_created_at(REPO, "v1", None) is None

    @rsps_lib.activate
    def test_tag_created_at_non_json_manifest_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            body=b"not-json{{",
            status=200,
        )
        assert tag_created_at(REPO, "v1", None) is None

    @rsps_lib.activate
    def test_tag_created_at_no_config_digest_returns_none(self):
        """Manifest with no config.digest → None."""
        manifest_no_digest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"mediaType": "application/vnd.docker.container.image.v1+json"},
        }
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            json=manifest_no_digest,
            status=200,
        )
        assert tag_created_at(REPO, "v1", None) is None

    @rsps_lib.activate
    def test_tag_created_at_blob_non_ok_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            json=_MANIFEST_V2,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/blobs/sha256:configabc",
            status=404,
        )
        assert tag_created_at(REPO, "v1", None) is None

    @rsps_lib.activate
    def test_tag_created_at_bad_datetime_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            json=_MANIFEST_V2,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/blobs/sha256:configabc",
            json={"created": "not-a-date"},
            status=200,
        )
        assert tag_created_at(REPO, "v1", None) is None

    @rsps_lib.activate
    def test_tag_created_at_oci_index_empty_manifests_returns_none(self):
        oci_empty = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [],
        }
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/idx",
            json=oci_empty,
            status=200,
        )
        assert tag_created_at(REPO, "idx", None) is None

    @rsps_lib.activate
    def test_tag_created_at_oci_index_no_child_digest_returns_none(self):
        oci_no_digest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json"}],
        }
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/idx",
            json=oci_no_digest,
            status=200,
        )
        assert tag_created_at(REPO, "idx", None) is None

    @rsps_lib.activate
    def test_tag_created_at_child_manifest_non_ok_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/idx",
            json=_OCI_INDEX,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/sha256:childmanifest",
            status=500,
        )
        assert tag_created_at(REPO, "idx", None) is None

    @rsps_lib.activate
    def test_tag_created_at_child_manifest_non_json_returns_none(self):
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/idx",
            json=_OCI_INDEX,
            status=200,
        )
        rsps_lib.add(
            rsps_lib.GET,
            f"https://{HOST}/v2/{PATH}/manifests/sha256:childmanifest",
            body=b"bad{{",
            status=200,
        )
        assert tag_created_at(REPO, "idx", None) is None

    # --- delete_tag edge cases ---

    @rsps_lib.activate
    def test_delete_tag_missing_content_digest_header_raises(self):
        rsps_lib.add(
            rsps_lib.HEAD,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            status=200,
            # No Docker-Content-Digest header
        )
        with pytest.raises(RegistryError, match="Docker-Content-Digest"):
            delete_tag(REPO, "v1", None)

    @rsps_lib.activate
    def test_delete_tag_non_405_error_raises(self):
        rsps_lib.add(
            rsps_lib.HEAD,
            f"https://{HOST}/v2/{PATH}/manifests/v1",
            status=200,
            headers={"Docker-Content-Digest": DIGEST},
        )
        rsps_lib.add(
            rsps_lib.DELETE,
            f"https://{HOST}/v2/{PATH}/manifests/{DIGEST}",
            status=500,
        )
        with pytest.raises(RegistryError, match="500"):
            delete_tag(REPO, "v1", None)

    # --- _split_repository ---

    def test_split_repository_no_slash_raises(self):
        from vgpu_driver_operator.registry import _split_repository

        with pytest.raises(RegistryError, match="no '/'"):
            _split_repository("noslash")

    # --- _parse_link_next ---

    def test_parse_link_next_no_next_rel(self):
        from vgpu_driver_operator.registry import _parse_link_next

        # Only a "prev" relation — should return None
        result = _parse_link_next('</v2/tags/list?last=a>; rel="prev"', HOST)
        assert result is None

    def test_parse_link_next_empty_header(self):
        from vgpu_driver_operator.registry import _parse_link_next

        assert _parse_link_next("", HOST) is None

    def test_parse_link_next_relative_url_is_resolved(self):
        from vgpu_driver_operator.registry import _parse_link_next

        result = _parse_link_next('</v2/tags/list?last=x>; rel="next"', HOST)
        assert result == f"https://{HOST}/v2/tags/list?last=x"
