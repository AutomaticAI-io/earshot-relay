"""Real MinIO tests create only one private, loopback-bound Docker fixture."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.request

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("minio_setup", HERE / "minio_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def cli(*args):
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": "/nonexistent", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p and "site-packages" in p), "AWS_ACCESS_KEY_ID": "DO-NOT-USE", "AWS_SECRET_ACCESS_KEY": "DO-NOT-USE", "AWS_EC2_METADATA_DISABLED": "true", "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "MC_HOST_setup": "http://bad:bad@127.0.0.1:1"}
    return subprocess.run([sys.executable, str(HERE / "minio_setup.py"), *map(str, args)], capture_output=True, text=True, env=env, timeout=120)


def write_json(path, obj, mode=0o600):
    path.write_text(json.dumps(obj))
    path.chmod(mode)


def manifest(workspace="trial"):
    return {"workspace": workspace, "bucket": "earshot-" + workspace, "channels": ["phone", "alarm"], "readers": {"speaker": ["phone"], "alerts": ["alarm"]}}


def s3(endpoint, creds=None, unsigned=False):
    return boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id=creds["access_key"] if creds else "dummy", aws_secret_access_key=creds["secret_key"] if creds else "dummy", config=Config(signature_version=UNSIGNED if unsigned else "s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 0}, connect_timeout=3, read_timeout=5, request_checksum_calculation="when_required", response_checksum_validation="when_required"))


class PrivateFixture(dict):
    def __repr__(self):
        return "<private disposable MinIO fixture>"


@pytest.fixture
def fixture(tmp_path):
    if os.environ.get("EARSHOT_MINIO_TESTS") != "1":
        pytest.skip("set EARSHOT_MINIO_TESTS=1 to run isolated Docker MinIO tests")
    name = "ears65-test-" + secrets.token_hex(8)
    admin = {"access_key": "adm" + secrets.token_hex(10), "secret_key": secrets.token_hex(24)}
    envfile = tmp_path / "docker.env"
    envfile.write_text("MINIO_ROOT_USER=" + admin["access_key"] + "\nMINIO_ROOT_PASSWORD=" + admin["secret_key"] + "\n")
    envfile.chmod(0o600)
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": str(tmp_path), "NO_PROXY": "*"}
    def docker(*args):
        return subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", *args], capture_output=True, text=True, env=env, timeout=60)
    created = False
    try:
        r = docker("run", "-d", "--pull=never", "--name", name, "--env-file", str(envfile), "-p", "127.0.0.1::9000", "minio/minio:latest", "server", "/data")
        assert r.returncode == 0, "could not start isolated MinIO container"
        created = True
        r = docker("port", name, "9000/tcp")
        match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})\s*", r.stdout)
        assert r.returncode == 0 and match
        port = int(match.group(1))
        assert 1 <= port <= 65535
        endpoint = "http://127.0.0.1:" + str(port)
        for _ in range(80):
            try:
                with urllib.request.urlopen(endpoint + "/minio/health/ready", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(.25)
        else:
            pytest.fail("isolated MinIO did not become ready")
        adminfile = tmp_path / "admin.json"
        write_json(adminfile, admin)
        yield PrivateFixture(endpoint=endpoint, admin=admin, adminfile=adminfile, docker=docker, name=name, dir=tmp_path)
    finally:
        if created:
            removed = docker("rm", "-f", name)
            assert removed.returncode == 0, "could not remove owned MinIO fixture"


def run_apply(f, m, state):
    assert re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", f["endpoint"])
    return cli("--spec", m, "--apply", "--endpoint", f["endpoint"], "--admin-file", f["adminfile"], "--state-dir", state)


def deny(fn):
    with pytest.raises(ClientError) as error:
        fn()
    assert error_code(error.value) in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403"}


def error_code(error):
    return error.response.get("Error", {}).get("Code")


def test_plan_is_offline_and_refuses_invalid_specs(tmp_path):
    path = tmp_path / "spec.json"
    write_json(path, manifest())
    before = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in tmp_path.iterdir()}
    state = tmp_path / "should-not-exist"
    result = cli("--spec", path, "--endpoint", "http://example.invalid", "--state-dir", state)
    assert result.returncode == 0 and json.loads(result.stdout)["side_effects"] is False
    assert before == {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in tmp_path.iterdir()}
    assert not state.exists()
    bad = manifest()
    bad["channels"] = ["a/*"]
    write_json(path, bad)
    assert cli("--spec", path).returncode != 0
    bad = manifest()
    bad["bucket"] = "earshot-another"
    write_json(path, bad)
    assert cli("--spec", path, "--apply", "--endpoint", "http://127.0.0.1:1", "--admin-file", tmp_path / "none", "--state-dir", state).returncode != 0
    assert not state.exists()
    write_json(path, manifest())
    assert cli("--spec", path, "--apply").returncode != 0
    bad_admin = tmp_path / "bad-admin.json"
    write_json(bad_admin, {"access_key": "test-admin", "secret_key": "test-secret"}, mode=0o644)
    refused = cli("--spec", path, "--apply", "--endpoint", "http://127.0.0.1:1", "--admin-file", bad_admin, "--state-dir", state)
    assert refused.returncode != 0 and not state.exists() and "test-secret" not in refused.stderr
    assert cli("--spec", path, "--apply", "--endpoint", "http://example.invalid", "--admin-file", tmp_path / "none", "--state-dir", state).returncode != 0
    assert not state.exists()


def test_real_minio_permissions_reapply_recovery_and_revocation(fixture):
    f = fixture
    m = f["dir"] / "spec.json"
    state = f["dir"] / "state"
    write_json(m, manifest())
    result = run_apply(f, m, state)
    assert result.returncode == 0, result.stderr
    journal = json.loads((state / "state.json").read_text())
    w = json.loads((state / "writer.json").read_text())
    r = json.loads((state / "reader-speaker.json").read_text())
    other = json.loads((state / "reader-alerts.json").read_text())
    for file in state.glob("*.json"):
        assert file.stat().st_mode & 0o077 == 0
        assert f["admin"]["secret_key"] not in file.read_text()
    for credential in [f["admin"], w, r, other]:
        assert credential["access_key"] not in result.stdout + result.stderr
        assert credential["secret_key"] not in result.stdout + result.stderr
    admin, writer, reader, alerts = [s3(f["endpoint"], x) for x in [f["admin"], w, r, other]]
    bucket = w["bucket"]
    admin.create_bucket(Bucket="earshot-elsewhere")
    admin.put_object(Bucket=bucket, Key="alarm/present", Body=b"alarm")
    writer.put_object(Bucket=bucket, Key="phone/present", Body=b"preserve-me")
    assert reader.get_object(Bucket=bucket, Key="phone/present")["Body"].read() == b"preserve-me"
    assert reader.list_objects_v2(Bucket=bucket, Prefix="phone/")["KeyCount"] == 1
    assert alerts.get_object(Bucket=bucket, Key="alarm/present")["Body"].read() == b"alarm"
    deny(lambda: writer.get_object(Bucket=bucket, Key="phone/present"))
    deny(lambda: writer.list_objects_v2(Bucket=bucket, Prefix="phone/"))
    deny(lambda: writer.delete_object(Bucket=bucket, Key="phone/present"))
    deny(lambda: writer.put_object(Bucket=bucket, Key="other/present", Body=b"x"))
    deny(lambda: writer.put_object(Bucket="earshot-elsewhere", Key="phone/present", Body=b"x"))
    deny(lambda: reader.get_object(Bucket=bucket, Key="alarm/present"))
    deny(lambda: reader.get_object(Bucket="earshot-elsewhere", Key="phone/present"))
    deny(lambda: reader.list_objects_v2(Bucket=bucket, Prefix="alarm/"))
    deny(lambda: reader.list_objects_v2(Bucket=bucket, Prefix=""))
    deny(lambda: reader.put_object(Bucket=bucket, Key="phone/x", Body=b"x"))
    anon = s3(f["endpoint"], unsigned=True)
    deny(lambda: anon.get_object(Bucket=bucket, Key="phone/present"))
    deny(lambda: anon.list_objects_v2(Bucket=bucket, Prefix="phone/"))
    deny(lambda: anon.put_object(Bucket=bucket, Key="phone/x", Body=b"x"))
    second = run_apply(f, m, state)
    assert second.returncode == 0, second.stderr
    assert journal["identities"] == json.loads((state / "state.json").read_text())["identities"]
    assert reader.get_object(Bucket=bucket, Key="phone/present")["Body"].read() == b"preserve-me"
    assert json.loads((state / "writer.json").read_text()) == w
    # The writer's S3 identity must not invoke MinIO administration.
    configdir = f["dir"] / "writer-mc"
    configdir.mkdir(mode=0o700)
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": str(f["dir"]), "NO_PROXY": "*"}
    base = ["mc", "--config-dir", str(configdir), "--json"]
    p = subprocess.run(base + ["alias", "import", "w"], input=json.dumps({"url": f["endpoint"], "accessKey": w["access_key"], "secretKey": w["secret_key"], "api": "s3v4", "path": "on"}), capture_output=True, text=True, env=env)
    assert p.returncode == 0
    p = subprocess.run(base + ["admin", "user", "add", "w"], input="unauthorized-" + secrets.token_hex(4) + "\n" + secrets.token_hex(20) + "\n", capture_output=True, text=True, env=env)
    assert p.returncode != 0
    cfg = f["dir"] / "admin-mc"
    cfg.mkdir(mode=0o700)
    base = ["mc", "--config-dir", str(cfg), "--json"]
    p = subprocess.run(base + ["alias", "import", "a"], input=json.dumps({"url": f["endpoint"], "accessKey": f["admin"]["access_key"], "secretKey": f["admin"]["secret_key"], "api": "s3v4", "path": "on"}), capture_output=True, text=True, env=env)
    assert p.returncode == 0
    p = subprocess.run(base + ["admin", "user", "disable", "a", r["access_key"]], capture_output=True, text=True, env=env)
    assert p.returncode == 0
    deny(lambda: reader.get_object(Bucket=bucket, Key="phone/present"))
    blocked = run_apply(f, m, state)
    assert blocked.returncode != 0 and "not complete" in blocked.stderr
    journal = json.loads((state / "state.json").read_text())
    assert not journal["complete"]
    assert journal["progress"].startswith("checking ")
    for credentials in (f["admin"], w, r, other):
        assert credentials["secret_key"] not in blocked.stdout + blocked.stderr
    deny(lambda: reader.get_object(Bucket=bucket, Key="phone/present"))


def test_failure_recovery_and_binding_refusals(fixture):
    f = fixture
    m = f["dir"] / "spec.json"
    state = f["dir"] / "state"
    write_json(m, manifest("recover"))
    # Interrupt a real apply at the policy step, after the bucket has been created and bound.
    script = """import importlib.util,sys
s=importlib.util.spec_from_file_location('setup',sys.argv[1]); m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def fail(*args): raise m.Refusal('injected interruption')
m.Minio.create_policy=fail
sys.exit(m.main(sys.argv[2:]))
"""
    runner = f["dir"] / "inject.py"
    runner.write_text(script)
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": "/nonexistent", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p and "site-packages" in p), "NO_PROXY": "*", "AWS_EC2_METADATA_DISABLED": "true"}
    args = ["--spec", str(m), "--apply", "--endpoint", f["endpoint"], "--admin-file", str(f["adminfile"]), "--state-dir", str(state)]
    p = subprocess.run([sys.executable, str(runner), str(HERE / "minio_setup.py"), *args], capture_output=True, text=True, env=env)
    assert p.returncode != 0 and "not complete" in p.stderr
    journal = json.loads((state / "state.json").read_text())
    assert journal["bucket_phase"] == "bound" and not journal["complete"]
    assert not (state / "writer.json").exists()
    admin = s3(f["endpoint"], f["admin"])
    p = run_apply(f, m, state)
    assert p.returncode == 0, p.stderr
    admin.put_object(Bucket="earshot-recover", Key="phone/preexisting", Body=b"keep")
    p = run_apply(f, m, state)
    assert p.returncode == 0, p.stderr
    assert admin.get_object(Bucket="earshot-recover", Key="phone/preexisting")["Body"].read() == b"keep"
    bad = manifest("recover")
    bad["readers"]["speaker"] = ["alarm"]
    write_json(m, bad)
    p = run_apply(f, m, state)
    assert p.returncode != 0 and "mismatch" in p.stderr
    assert admin.get_object(Bucket="earshot-recover", Key="phone/preexisting")["Body"].read() == b"keep"
    write_json(m, manifest("occupied"))
    admin.create_bucket(Bucket="earshot-occupied")
    p = run_apply(f, m, f["dir"] / "occupied")
    assert p.returncode != 0 and "adoption" in p.stderr
    admin.head_bucket(Bucket="earshot-occupied")
