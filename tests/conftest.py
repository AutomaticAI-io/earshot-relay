from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import boto3
import pytest
import uvicorn
from moto.server import ThreadedMotoServer
from starlette.testclient import TestClient

from earshot_relay.api import create_app
from earshot_relay.config import load_settings
from earshot_relay.service import Relay
from earshot_relay.storage import S3Writer
from earshot_relay.store import Store
from earshot_relay.tts import TtsClient

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "t" * 40
BEACON_TOKEN = "b" * 40
BUCKET = "earshot-test"

_PRIVATE_TMP: Path | None = None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Run every test under a private temp root, not the system /tmp.

    The relay's control store refuses a directory with any setuid/setgid/sticky
    parent (`control._safe_directory`), and /tmp is sticky (1777) on Linux and
    on GitHub runners. Under the default pytest basetemp every scoped-control test
    failed with "unsafe control directory" (57 failed, 50 errors on main 928c64a)
    although the code is right. A private root under the user's cache directory has
    no sticky parent. Subprocesses inherit it through TMPDIR. An explicit --basetemp
    still wins.
    """
    global _PRIVATE_TMP
    if config.option.basetemp:
        return
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    root = cache / "earshot-relay-tests"
    _PRIVATE_TMP = root / f"run-{os.getpid()}"
    for directory in (root, _PRIVATE_TMP):
        # Explicit chmod: mkdir(mode=) is masked by the umask (002 leaves 775, which
        # the control store rejects as group-writable).
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    config.option.basetemp = str(_PRIVATE_TMP / "pytest")
    os.environ["TMPDIR"] = str(_PRIVATE_TMP)
    tempfile.tempdir = str(_PRIVATE_TMP)


def pytest_unconfigure(config):
    if _PRIVATE_TMP is not None:
        shutil.rmtree(_PRIVATE_TMP, ignore_errors=True)


@pytest.fixture(scope="session")
def moto_endpoint():
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    admin = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="admin",
                         aws_secret_access_key="admin")
    admin.create_bucket(Bucket=BUCKET)
    yield endpoint
    server.stop()


@pytest.fixture(scope="session")
def s3_admin(moto_endpoint):
    return boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1", aws_access_key_id="admin",
                        aws_secret_access_key="admin")


def base_env(endpoint: str, data_dir: str, **extra: str) -> dict[str, str]:
    env = {
        "EARSHOT_S3_ENDPOINT": endpoint,
        "EARSHOT_BUCKET": BUCKET,
        "EARSHOT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "writer-key-id",
        "AWS_SECRET_ACCESS_KEY": "writer-secret-value",
        "EARSHOT_RELAY_TOKEN": TOKEN,
        "EARSHOT_RELAY_BEACON_TOKEN": BEACON_TOKEN,
        "EARSHOT_RELAY_ALLOW_HTTP_S3": "1",
        "EARSHOT_RELAY_DATA_DIR": data_dir,
        "EARSHOT_RELAY_PUBLIC_URL": "https://relay.example.com",
    }
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture
def make_relay(moto_endpoint, tmp_path):
    made: list[Store] = []

    def factory(*, tts_transport=None, watchdog_transport=None, clock=None, monotonic=None, **extra: str) -> Relay:
        settings = load_settings(base_env(moto_endpoint, str(tmp_path / f"data{len(made)}"), **extra), check_ffmpeg=False)
        store = Store(settings.db_path)
        made.append(store)
        tts = TtsClient(settings.tts, settings.max_audio_bytes, transport=tts_transport) if settings.tts else None
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        if monotonic is not None:
            kwargs["monotonic"] = monotonic
        if watchdog_transport is not None:
            kwargs["watchdog_transport"] = watchdog_transport
        return Relay(settings, store, S3Writer(settings), tts, **kwargs)

    yield factory
    for s in made:
        s.close()


@pytest.fixture
def relay(make_relay):
    return make_relay()


@pytest.fixture
def client(relay):
    with TestClient(create_app(relay), base_url="http://relay.test") as c:
        yield c


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def golden_beacons() -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / "beta-0.1.11-receipts.jsonl").read_text().splitlines() if line]


@pytest.fixture
def live_relay(relay):
    config = uvicorn.Config(create_app(relay), host="127.0.0.1", port=0, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
