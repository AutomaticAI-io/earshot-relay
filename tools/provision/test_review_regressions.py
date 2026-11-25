"""Security regressions at the provisioner's provider and TLS boundaries."""
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

import pytest

from test_minio_setup import HERE, cli, deny, fixture, manifest, run_apply, s3, setup, write_json


def test_validation_rejects_alarm_only_and_explicit_port_zero(tmp_path):
    specfile = tmp_path / "spec.json"
    state = tmp_path / "never-created"
    adminfile = tmp_path / "admin.json"
    write_json(adminfile, {"access_key": "fake-admin", "secret_key": "fake-secret"})
    bad = manifest("alarm-only")
    bad["channels"] = ["alarm"]
    bad["readers"] = {"speaker": ["alarm"]}
    write_json(specfile, bad)
    planned = cli("--spec", specfile)
    assert planned.returncode != 0 and "non-alarm" in planned.stderr
    attempted = cli("--spec", specfile, "--apply", "--endpoint", "http://127.0.0.1:1", "--admin-file", adminfile, "--state-dir", state)
    assert attempted.returncode != 0 and "non-alarm" in attempted.stderr
    assert not state.exists()
    write_json(specfile, manifest("portzero"))
    for endpoint in ("http://127.0.0.1:0", "https://127.0.0.1:0", "https://[::1]:0", "http://127.0.0.1:"):
        result = cli("--spec", specfile, "--apply", "--endpoint", endpoint, "--admin-file", adminfile, "--state-dir", state)
        assert result.returncode != 0 and "port must be" in result.stderr
        assert not state.exists()
    assert setup.endpoint_url("http://127.0.0.1:80") == "http://127.0.0.1:80"
    assert setup.endpoint_url("https://example.test") == "https://example.test"


def admin_mc(f):
    cfg = f["dir"] / "regression-mc"
    cfg.mkdir(mode=0o700)
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": str(f["dir"]), "NO_PROXY": "*"}
    cmd = ["mc", "--config-dir", str(cfg), "--json"]
    alias = json.dumps({"url": f["endpoint"], "accessKey": f["admin"]["access_key"], "secretKey": f["admin"]["secret_key"], "api": "s3v4", "path": "on"})
    p = subprocess.run(cmd + ["alias", "import", "fixture"], input=alias, capture_output=True, text=True, env=env)
    assert p.returncode == 0
    def invoke(*args):
        return subprocess.run(cmd + list(args), capture_output=True, text=True, env=env, timeout=20)
    return invoke


def crash_script(f, specfile, state, injected):
    assert re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", f["endpoint"])
    runner = f["dir"] / "crash.py"
    runner.write_text("""import importlib.util, os, sys
s=importlib.util.spec_from_file_location('setup',sys.argv[1]); m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
""" + injected + "\nsys.exit(m.main(sys.argv[2:]))\n")
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": "/nonexistent", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p and "site-packages" in p), "NO_PROXY": "*", "AWS_EC2_METADATA_DISABLED": "true"}
    args = ["--spec", str(specfile), "--apply", "--endpoint", f["endpoint"], "--admin-file", str(f["adminfile"]), "--state-dir", str(state)]
    process = subprocess.Popen([sys.executable, str(runner), str(HERE / "minio_setup.py"), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    return process


def wait_stopped(process):
    for _ in range(250):
        if process.poll() is not None:
            pytest.fail("injected process exited before reaching its crash point")
        try:
            status = Path(f"/proc/{process.pid}/status").read_text()
        except FileNotFoundError:
            pytest.fail("injected process vanished before reaching its crash point")
            return
        if re.search(r"^State:\s+T", status, flags=re.MULTILINE):
            return
        time.sleep(.04)
    pytest.fail("injected process did not reach its crash point")


def kill_owned(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=10)


def test_created_user_revoked_in_crash_window_is_not_resurrected(fixture):
    f = fixture
    manifest_file, state = f["dir"] / "spec.json", f["dir"] / "state"
    write_json(manifest_file, manifest("crashuser"))
    injected = """original=m.Minio.add_user
def pause(self, identity):
 original(self, identity)
 os.kill(os.getpid(), 19)
m.Minio.add_user=pause
"""
    process = crash_script(f, manifest_file, state, injected)
    identity = None
    try:
        wait_stopped(process)
        journal = json.loads((state / "state.json").read_text())
        label = next(k for k, v in journal["identities"].items() if v["user_attempted"])
        identity = journal["identities"][label]
        assert not identity["user_confirmed"] and not journal["complete"]
        mc = admin_mc(f)
        assert mc("admin", "user", "info", "fixture", identity["access_key"]).returncode == 0
    finally:
        kill_owned(process)
    assert identity is not None
    assert mc("admin", "user", "remove", "fixture", identity["access_key"]).returncode == 0
    result = run_apply(f, manifest_file, state)
    assert result.returncode != 0 and "attempted user is missing" in result.stderr
    assert mc("admin", "user", "info", "fixture", identity["access_key"]).returncode != 0
    updated = json.loads((state / "state.json").read_text())
    assert not updated["complete"] and updated["progress"] == "attempted user missing for " + label
    assert not (state / "writer.json").exists()
    deny(lambda: s3(f["endpoint"], identity).list_objects_v2(Bucket="earshot-crashuser", Prefix=identity["channels"][0] + "/"))
    assert identity["secret_key"] not in result.stdout + result.stderr


def test_unconfirmed_attached_user_requires_reconciliation(fixture):
    f = fixture
    manifest_file, state = f["dir"] / "spec.json", f["dir"] / "state"
    write_json(manifest_file, manifest("crashattach"))
    injected = """original=m.Minio.attach
def pause(self, identity):
 original(self, identity)
 os.kill(os.getpid(), 19)
m.Minio.attach=pause
"""
    process = crash_script(f, manifest_file, state, injected)
    try:
        wait_stopped(process)
        journal = json.loads((state / "state.json").read_text())
        label = next(k for k, v in journal["identities"].items() if v["user_attempted"])
        identity = journal["identities"][label]
        assert not identity["user_confirmed"] and not identity["attachment_confirmed"]
        mc = admin_mc(f)
        info = mc("admin", "user", "info", "fixture", identity["access_key"])
        assert info.returncode == 0 and json.loads(info.stdout).get("policyName") == identity["policy"]
    finally:
        kill_owned(process)
    result = run_apply(f, manifest_file, state)
    assert result.returncode != 0 and "remains unconfirmed" in result.stderr
    assert not json.loads((state / "state.json").read_text())["complete"]
    assert mc("admin", "user", "remove", "fixture", identity["access_key"]).returncode == 0
    result = run_apply(f, manifest_file, state)
    assert result.returncode != 0 and "attempted user is missing" in result.stderr
    assert mc("admin", "user", "info", "fixture", identity["access_key"]).returncode != 0
    deny(lambda: s3(f["endpoint"], identity).list_objects_v2(Bucket="earshot-crashattach", Prefix=identity["channels"][0] + "/"))
    assert not json.loads((state / "state.json").read_text())["complete"]


def test_replaced_unbound_bucket_is_not_adopted(fixture):
    f = fixture
    manifest_file, state = f["dir"] / "spec.json", f["dir"] / "state"
    write_json(manifest_file, manifest("replacement"))
    injected = """original=m.Minio.__init__
def init(self,*args):
 original(self,*args)
 put=self.s3.put_object
 def pause(**kwargs):
  os.kill(os.getpid(), 19)
  return put(**kwargs)
 self.s3.put_object=pause
m.Minio.__init__=init
"""
    process = crash_script(f, manifest_file, state, injected)
    try:
        wait_stopped(process)
        journal = json.loads((state / "state.json").read_text())
        assert journal["bucket_phase"] == "created" and not journal["complete"]
        admin = s3(f["endpoint"], f["admin"])
        bucket = "earshot-replacement"
        admin.head_bucket(Bucket=bucket)
        assert admin.list_objects_v2(Bucket=bucket).get("KeyCount") == 0
    finally:
        kill_owned(process)
    admin.delete_bucket(Bucket=bucket)
    admin.create_bucket(Bucket=bucket)
    admin.put_object(Bucket=bucket, Key="phone/foreign", Body=b"foreign-data")
    result = run_apply(f, manifest_file, state)
    assert result.returncode != 0 and "continuity unknown" in result.stderr
    updated = json.loads((state / "state.json").read_text())
    assert updated["bucket_phase"] == "created" and not updated["complete"]
    assert updated["progress"] == "bucket continuity unknown"
    assert admin.get_object(Bucket=bucket, Key="phone/foreign")["Body"].read() == b"foreign-data"
    assert [obj["Key"] for obj in admin.list_objects_v2(Bucket=bucket).get("Contents", [])] == ["phone/foreign"]
    mc = admin_mc(f)
    for identity in journal["identities"].values():
        assert mc("admin", "user", "info", "fixture", identity["access_key"]).returncode != 0
    assert not (state / "writer.json").exists()


def test_marker_written_but_binding_not_confirmed_requires_reconciliation(fixture):
    f = fixture
    manifest_file, state = f["dir"] / "spec.json", f["dir"] / "state"
    write_json(manifest_file, manifest("markerwritten"))
    injected = """original=m.Minio.__init__
def init(self,*args):
 original(self,*args)
 put=self.s3.put_object
 def pause(**kwargs):
  result=put(**kwargs)
  os.kill(os.getpid(), 19)
  return result
 self.s3.put_object=pause
m.Minio.__init__=init
"""
    process = crash_script(f, manifest_file, state, injected)
    try:
        wait_stopped(process)
        journal = json.loads((state / "state.json").read_text())
        assert journal["bucket_phase"] == "created" and not journal["complete"]
        admin = s3(f["endpoint"], f["admin"])
        obj = admin.get_object(Bucket="earshot-markerwritten", Key=".earshot-provisioner/binding.json")
        assert json.loads(obj["Body"].read())["binding"] == journal["binding"]
    finally:
        kill_owned(process)
    result = run_apply(f, manifest_file, state)
    assert result.returncode != 0 and "continuity unknown" in result.stderr
    updated = json.loads((state / "state.json").read_text())
    assert not updated["complete"] and updated["bucket_phase"] == "created"
    assert not (state / "writer.json").exists()


def test_inherited_tls_keylog_is_suppressed_during_real_https(tmp_path):
    control_keylog = tmp_path / "positive-control-keylog"
    control_env = {"PATH": os.defpath, "HOME": "/nonexistent", "SSLKEYLOGFILE": str(control_keylog)}
    control = subprocess.run([sys.executable, "-c", "import ssl; ssl.create_default_context()"], capture_output=True, env=control_env, timeout=10)
    control_created = control_keylog.exists()
    if control_keylog.is_file():
        control_keylog.unlink()
    assert control.returncode == 0 and control_created, "Python TLS key-log control did not activate"
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    result = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    key.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    requests = []
    errors = []
    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                with conn, context.wrap_socket(conn, server_side=True) as tls:
                    tls.settimeout(3)
                    request = b""
                    while b"\r\n\r\n" not in request and len(request) < 65536:
                        part = tls.recv(4096)
                        if not part:
                            break
                        request += part
                    requests.append(request)
                    body = b'<Error><Code>AccessDenied</Code><Message>Denied</Message></Error>'
                    tls.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/xml\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except (OSError, ssl.SSLError) as exc:
                errors.append(type(exc).__name__)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    specfile, adminfile, state = tmp_path / "spec.json", tmp_path / "admin.json", tmp_path / "state"
    write_json(specfile, manifest("tlsfixture"))
    write_json(adminfile, {"access_key": "fixture-access", "secret_key": "fixture-secret-not-real"})
    keylog = tmp_path / "unprotected-keylog"
    runner = tmp_path / "tls_runner.py"
    runner.write_text("""import importlib.util, sys
import botocore.httpsession
s=importlib.util.spec_from_file_location('setup',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
original=m.Minio.__init__
def init(self,*args):
 botocore.httpsession.where=lambda: sys.argv[2]
 return original(self,*args)
m.Minio.__init__=init
sys.exit(m.main(sys.argv[3:]))
""")
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": "/nonexistent", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p and "site-packages" in p), "SSLKEYLOGFILE": str(keylog), "AWS_EC2_METADATA_DISABLED": "true", "NO_PROXY": "*", "AWS_ACCESS_KEY_ID": "bad-fallback", "AWS_SECRET_ACCESS_KEY": "bad-fallback-secret", "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}
    args = ["--spec", str(specfile), "--apply", "--endpoint", f"https://127.0.0.1:{port}", "--admin-file", str(adminfile), "--state-dir", str(state)]
    try:
        result = subprocess.run([sys.executable, str(runner), str(HERE / "minio_setup.py"), str(cert), *args], capture_output=True, text=True, env=env, timeout=30)
    finally:
        stop.set()
        listener.close()
        thread.join(5)
        logged = keylog.exists()
        if keylog.is_file():
            keylog.unlink()
    assert result.returncode != 0 and "not complete" in result.stderr
    assert requests and not errors, "test must complete a verified TLS handshake and reach its fixture"
    assert b"fixture-access" in requests[0] and b"bad-fallback" not in requests[0]
    assert b"fixture-secret-not-real" not in requests[0]
    assert not logged, "inherited key logging was not disabled"
    assert "fixture-secret-not-real" not in result.stdout + result.stderr
    assert json.loads((state / "state.json").read_text())["bucket_phase"] == "new"
