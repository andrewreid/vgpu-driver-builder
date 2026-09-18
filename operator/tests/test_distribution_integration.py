"""Opt-in test against a disposable Distribution binary, never an existing registry.

VGPU_TEST_REGISTRY_BINARY=/absolute/path/to/registry PYTHONPATH=src pytest \
    tests/test_distribution_integration.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
from datetime import datetime, timezone

import pytest
import requests

from vgpu_driver_operator.gc import run

IMAGE = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"


@pytest.fixture
def distribution(tmp_path, monkeypatch):
    binary = os.environ.get("VGPU_TEST_REGISTRY_BINARY")
    if not binary:
        pytest.skip("Set VGPU_TEST_REGISTRY_BINARY to run the disposable registry test")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True)
    config = tmp_path / "config.yml"
    config.write_text(f"""version: 0.1
log:
  level: error
storage:
  filesystem:
    rootdirectory: {tmp_path / 'data'}
  delete:
    enabled: true
  maintenance:
    uploadpurging:
      enabled: false
http:
  addr: 127.0.0.1:{port}
  secret: disposable-retention-test
  tls:
    certificate: {cert}
    key: {key}
""")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    host = f"127.0.0.1:{port}"
    url = f"https://{host}"
    log = tmp_path / "registry.log"
    with log.open("w") as output:
        process = subprocess.Popen([str(Path(binary).resolve()), "serve", str(config)],
                                   stdout=output, stderr=output,
                                   env={**os.environ, "OTEL_TRACES_EXPORTER": "none"})
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(log.read_text())
                try:
                    if requests.get(url + "/v2/", timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Disposable registry did not start: " + log.read_text())
            yield host, url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_real_anonymous_retention_preserves_aliases_indexes_and_required_images(distribution):
    host, url = distribution
    repository = host + "/retention-test"
    base = url + "/v2/retention-test"

    def upload_blob(data):
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        response = requests.post(base + "/blobs/uploads/", timeout=10)
        response.raise_for_status()
        location = response.headers["Location"]
        response = requests.put(location, params={"digest": digest}, data=data,
                                headers={"Content-Type": "application/octet-stream"}, timeout=10)
        response.raise_for_status()
        return digest

    def put_manifest(tag, body):
        data = json.dumps(body, separators=(",", ":")).encode()
        response = requests.put(base + "/manifests/" + tag, data=data,
                                headers={"Content-Type": body["mediaType"]}, timeout=10)
        response.raise_for_status()
        return response.headers["Docker-Content-Digest"], len(data)

    def image(seed):
        config = json.dumps({"created": "2020-01-01T00:00:00Z", "architecture": "amd64",
                             "os": "linux", "config": {"Labels": {"test": seed}},
                             "rootfs": {"type": "layers", "diff_ids": []}}).encode()
        return {"schemaVersion": 2, "mediaType": IMAGE, "layers": [], "config": {
            "mediaType": CONFIG, "digest": upload_blob(config), "size": len(config),
        }}

    def tag(version):
        return "550.54.15-6.12.1-flatcar-flatcar" + version

    protected = [tag(v) for v in ["4757.2.0", "4593.2.3", "4593.2.2", "5000.2.0"]]
    for name in protected:
        put_manifest(name, image(name))
    removed = [tag("4230.2.0")]
    put_manifest(removed[0], image("stale"))
    alias_body = image("protected-alias")
    for name in [tag("4230.2.1"), "release"]:
        put_manifest(name, alias_body)
        protected.append(name)
    child_tag = tag("4230.2.2")
    child, size = put_manifest(child_tag, image("index-child"))
    put_manifest("protected-index", {"schemaVersion": 2, "mediaType": INDEX, "manifests": [{
        "mediaType": IMAGE, "digest": child, "size": size,
        "platform": {"architecture": "amd64", "os": "linux"},
    }]})
    protected.extend([child_tag, "protected-index", child])
    eligible_alias_body = image("eligible-alias")
    for name in [tag("4230.2.3"), tag("4230.2.4")]:
        put_manifest(name, eligible_alias_body)
        removed.append(name)
    result = run(
        {"registry": {"repository": repository}, "precompile": True,
         "flatcar": {"versions": ["4593.2.2"]},
         "retention": {"enabled": True, "keepPreviousFlatcarVersions": 1}},
        {"observedNodes": [{"flatcarVersion": "4757.2.0"}],
         "trackedChannelVersions": [{"flatcarVersion": "5000.2.0"}]},
        auth=None, now=datetime.now(timezone.utc), logger=logging.getLogger(__name__),
        emit_event=lambda *args, **kwargs: None,
    )
    assert result["retention"]["result"] == "Succeeded", result
    assert result["retention"]["deletedCount"] == 3
    assert {entry["tag"] for entry in result["pruned"]} == {
        repository + ":" + name for name in removed
    }
    for name in removed:
        assert requests.get(base + "/manifests/" + name, headers={"Accept": IMAGE},
                            timeout=10).status_code == 404
    for name in protected:
        response = requests.get(base + "/manifests/" + name,
                                headers={"Accept": f"{IMAGE}, {INDEX}"}, timeout=10)
        assert response.status_code == 200, (name, response.text)
        body = response.json()
        if "config" in body:
            assert requests.get(base + "/blobs/" + body["config"]["digest"],
                                timeout=10).status_code == 200
