"""Provider compatibility (spec section 15.4): the relay's PutObject against real S3-compatible servers.

SeaweedFS and Garage run in throwaway Docker containers. Each test sends text and audio through the HTTP API with a
write-only key, then reads the objects back with a separate read key and checks body and Content-Type. This proves
the botocore settings the relay uses (path-style, SigV4, `when_required` checksums) work beyond AWS and moto.

Skipped unless EARSHOT_COMPAT_PROVIDERS=1 (needs Docker and network access to pull the images):

    EARSHOT_COMPAT_PROVIDERS=1 uv run pytest -q tests/test_providers.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.config import load_settings
from earshot_relay.service import Relay
from earshot_relay.storage import S3Writer
from earshot_relay.store import Store

SEAWEEDFS_IMAGE = "chrislusf/seaweedfs:3.80"
GARAGE_IMAGE = "dxflrs/garage:v1.0.1"
BUCKET = "earshot"
TOKEN = "p" * 40

pytestmark = pytest.mark.skipif(
    os.environ.get("EARSHOT_COMPAT_PROVIDERS") != "1" or shutil.which("docker") is None,
    reason="set EARSHOT_COMPAT_PROVIDERS=1 (needs Docker) to run the provider compatibility tests",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True).stdout


def s3(endpoint: str, region: str, key: str, secret: str):
    return boto3.client("s3", endpoint_url=endpoint, region_name=region, aws_access_key_id=key,
                        aws_secret_access_key=secret,
                        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4",
                                      request_checksum_calculation="when_required",
                                      response_checksum_validation="when_required",
                                      retries={"max_attempts": 1}))


def wait_until(fn, what: str, timeout: float = 90) -> None:
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            if fn():
                return
        except Exception as exc:  # noqa: BLE001 - the server is still starting
            last = exc
        time.sleep(1)
    raise RuntimeError(f"{what} did not become ready: {last!r}")


def start_seaweedfs(tmp: Path) -> dict:
    """weed server -s3 with two identities: a writer that may only Write to the bucket, and an admin."""
    writer = ("seaweed-writer", secrets.token_hex(16))
    admin = ("seaweed-admin", secrets.token_hex(16))
    (tmp / "s3.json").write_text(json.dumps({"identities": [
        {"name": "admin", "credentials": [{"accessKey": admin[0], "secretKey": admin[1]}],
         "actions": ["Admin", "Read", "List", "Tagging", "Write"]},
        {"name": "writer", "credentials": [{"accessKey": writer[0], "secretKey": writer[1]}],
         "actions": [f"Write:{BUCKET}"]},
    ]}))
    os.chmod(tmp / "s3.json", 0o644)
    port = free_port()
    name = f"earshot-compat-seaweedfs-{secrets.token_hex(3)}"
    docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8333", "-v", f"{tmp}:/cfg:ro", SEAWEEDFS_IMAGE,
           "server", "-s3", "-s3.config=/cfg/s3.json", "-dir=/data", "-master.volumeSizeLimitMB=64", "-volume.max=8")
    endpoint = f"http://127.0.0.1:{port}"
    adm = s3(endpoint, "us-east-1", *admin)
    wait_until(lambda: adm.create_bucket(Bucket=BUCKET) is not None, "SeaweedFS S3 (create bucket)")
    return {"name": name, "endpoint": endpoint, "region": "us-east-1", "writer": writer, "reader": admin}


def start_garage(tmp: Path) -> dict:
    """A single-node Garage with a write-only key for the relay and a read-only key for the checks."""
    (tmp / "garage.toml").write_text("\n".join([
        'metadata_dir = "/var/lib/garage/meta"', 'data_dir = "/var/lib/garage/data"', 'db_engine = "sqlite"',
        "replication_factor = 1", 'rpc_bind_addr = "[::]:3901"', 'rpc_public_addr = "127.0.0.1:3901"',
        f'rpc_secret = "{secrets.token_hex(32)}"', "", "[s3_api]", 's3_region = "garage"',
        'api_bind_addr = "[::]:3900"', 'root_domain = ".s3.garage.localhost"', "",
    ]))
    os.chmod(tmp / "garage.toml", 0o644)
    port = free_port()
    name = f"earshot-compat-garage-{secrets.token_hex(3)}"
    docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:3900", "-v", f"{tmp / 'garage.toml'}:/etc/garage.toml:ro",
           GARAGE_IMAGE)
    node: dict = {}

    def node_id() -> bool:
        m = re.search(r"^([0-9a-f]{16})\s", docker("exec", name, "/garage", "status"), re.M)
        if m:
            node["id"] = m.group(1)
        return bool(m)

    wait_until(node_id, "Garage node")
    docker("exec", name, "/garage", "layout", "assign", "-z", "dc1", "-c", "1G", node["id"])
    docker("exec", name, "/garage", "layout", "apply", "--version", "1")
    docker("exec", name, "/garage", "bucket", "create", BUCKET)

    def key(label: str, *perms: str) -> tuple[str, str]:
        out = docker("exec", name, "/garage", "key", "create", label)
        key_id = re.search(r"Key ID:\s*(\S+)", out).group(1)
        secret = re.search(r"Secret key:\s*(\S+)", out).group(1)
        docker("exec", name, "/garage", "bucket", "allow", *perms, BUCKET, "--key", label)
        return key_id, secret

    writer, reader = key("relay-writer", "--write"), key("phone-reader", "--read")
    endpoint = f"http://127.0.0.1:{port}"
    rd = s3(endpoint, "garage", *reader)
    wait_until(lambda: rd.list_objects_v2(Bucket=BUCKET) is not None, "Garage S3 API")
    return {"name": name, "endpoint": endpoint, "region": "garage", "writer": writer, "reader": reader}


@pytest.fixture(scope="module", params=["seaweedfs", "garage"])
def provider(request):
    tmp = Path(tempfile.mkdtemp(prefix=f"earshot-compat-{request.param}-"))
    info = None
    try:
        info = start_seaweedfs(tmp) if request.param == "seaweedfs" else start_garage(tmp)
        yield info
    finally:
        if info:
            docker("rm", "-f", info["name"], check=False)
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def relay_client(provider, tmp_path):
    settings = load_settings({
        "EARSHOT_S3_ENDPOINT": provider["endpoint"], "EARSHOT_BUCKET": BUCKET, "EARSHOT_REGION": provider["region"],
        "AWS_ACCESS_KEY_ID": provider["writer"][0], "AWS_SECRET_ACCESS_KEY": provider["writer"][1],
        "EARSHOT_RELAY_TOKEN": TOKEN, "EARSHOT_RELAY_ALLOW_HTTP_S3": "1", "EARSHOT_RELAY_DATA_DIR": str(tmp_path),
    }, check_ffmpeg=False)
    store = Store(settings.db_path)
    try:
        with TestClient(create_app(Relay(settings, store, S3Writer(settings)))) as client:
            yield client
    finally:
        store.close()


def test_text_and_audio_land_with_the_right_body_and_type(provider, relay_client):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    reader = s3(provider["endpoint"], provider["region"], *provider["reader"])

    text = relay_client.post("/v1/messages", headers=auth, json={"text": "Provider check éè \U0001F600"})
    assert text.status_code == 202, text.text
    obj = reader.get_object(Bucket=BUCKET, Key=text.json()["key"])
    assert obj["Body"].read() == "Provider check éè \U0001F600".encode()
    assert obj["ContentType"] == "text/plain; charset=utf-8"

    audio_bytes = b"OggS" + secrets.token_bytes(64 * 1024)
    audio = relay_client.post("/v1/messages", headers=auth, files={"file": ("clip.ogg", audio_bytes, "audio/ogg")})
    assert audio.status_code == 202, audio.text
    obj = reader.get_object(Bucket=BUCKET, Key=audio.json()["key"])
    assert obj["Body"].read() == audio_bytes
    assert obj["ContentType"] == "audio/ogg"


def test_the_writer_key_cannot_read_back(provider, relay_client):
    """The relay only needs PutObject; these providers can enforce that, so the writer is refused a read."""
    from botocore.exceptions import ClientError

    sent = relay_client.post("/v1/messages", headers={"Authorization": f"Bearer {TOKEN}"}, json={"text": "write only"})
    assert sent.status_code == 202, sent.text
    writer = s3(provider["endpoint"], provider["region"], *provider["writer"])
    with pytest.raises(ClientError):
        writer.get_object(Bucket=BUCKET, Key=sent.json()["key"])


def test_doctor_write_succeeds(relay_client):
    r = relay_client.post("/v1/doctor", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["write"]["ok"] is True, r.text
