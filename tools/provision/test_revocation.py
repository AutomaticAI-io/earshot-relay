import json
import os
import subprocess

from test_minio_setup import fixture, manifest, run_apply, s3, write_json


def test_confirmed_identity_removal_is_not_recreated(fixture):
    f = fixture
    manifest_file = f["dir"] / "spec.json"
    state = f["dir"] / "state"
    write_json(manifest_file, manifest("revoked"))
    result = run_apply(f, manifest_file, state)
    assert result.returncode == 0
    reader = json.loads((state / "reader-speaker.json").read_text())
    cfg = f["dir"] / "admin-mc"
    cfg.mkdir(mode=0o700)
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": str(f["dir"]), "NO_PROXY": "*"}
    base = ["mc", "--config-dir", str(cfg), "--json"]
    result = subprocess.run(base + ["alias", "import", "a"], input=json.dumps({"url": f["endpoint"], "accessKey": f["admin"]["access_key"], "secretKey": f["admin"]["secret_key"], "api": "s3v4", "path": "on"}), capture_output=True, text=True, env=env)
    assert result.returncode == 0
    result = subprocess.run(base + ["admin", "user", "remove", "a", reader["access_key"]], capture_output=True, text=True, env=env)
    assert result.returncode == 0
    result = run_apply(f, manifest_file, state)
    assert result.returncode != 0 and "confirmed user is missing" in result.stderr
    from test_minio_setup import deny
    client = s3(f["endpoint"], reader)
    deny(lambda: client.list_objects_v2(Bucket=reader["bucket"], Prefix="phone/"))
