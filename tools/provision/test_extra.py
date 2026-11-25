import json
import os
import subprocess
import sys

from test_minio_setup import HERE, fixture, manifest, run_apply, s3, write_json


def test_unknown_bucket_create_and_binding_mismatch_fail_closed(fixture):
    f = fixture
    m = f["dir"] / "spec.json"
    write_json(m, manifest("uncertain"))
    state = f["dir"] / "state"
    script = """import importlib.util,sys
s=importlib.util.spec_from_file_location('setup',sys.argv[1]); m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
original=m.Minio.__init__
def init(self,*args):
 original(self,*args)
 create=self.s3.create_bucket
 def fail(**kwargs):
  create(**kwargs)
  raise m.Refusal('injected unknown outcome')
 self.s3.create_bucket=fail
m.Minio.__init__=init
sys.exit(m.main(sys.argv[2:]))
"""
    runner = f["dir"] / "unknown.py"
    runner.write_text(script)
    env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": "/nonexistent", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p and "site-packages" in p), "NO_PROXY": "*", "AWS_EC2_METADATA_DISABLED": "true"}
    args = ["--spec", str(m), "--apply", "--endpoint", f["endpoint"], "--admin-file", str(f["adminfile"]), "--state-dir", str(state)]
    p = subprocess.run([sys.executable, str(runner), str(HERE / "minio_setup.py"), *args], capture_output=True, text=True, env=env)
    assert p.returncode != 0 and "not complete" in p.stderr
    journal = json.loads((state / "state.json").read_text())
    assert journal["bucket_phase"] == "creating" and not journal["complete"]
    admin = s3(f["endpoint"], f["admin"])
    admin.head_bucket(Bucket="earshot-uncertain")
    p = run_apply(f, m, state)
    assert p.returncode != 0 and "outcome unknown" in p.stderr
    assert not (state / "writer.json").exists()
    write_json(m, manifest("bound"))
    bound = f["dir"] / "bound"
    assert run_apply(f, m, bound).returncode == 0
    admin.put_object(Bucket="earshot-bound", Key=".earshot-provisioner/binding.json", Body=b'{}')
    blocked = run_apply(f, m, bound)
    assert blocked.returncode != 0 and "binding mismatch" in blocked.stderr
    assert not json.loads((bound / "state.json").read_text())["complete"]
    assert admin.get_object(Bucket="earshot-bound", Key=".earshot-provisioner/binding.json")["Body"].read() == b'{}'
