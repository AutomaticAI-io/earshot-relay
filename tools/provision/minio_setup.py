#!/usr/bin/env python3
"""Standalone, deliberately narrow MinIO workspace provisioner; see README.md."""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

CHANNEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
WORKSPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,52}[a-z0-9])?\Z")
BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")


RENAME_NOREPLACE = 1
AT_FDCWD = -100


class Refusal(Exception):
    pass


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != {"workspace", "bucket", "channels", "readers"}:
        raise Refusal("spec must contain exactly workspace, bucket, channels and readers")
    workspace, bucket, channels, readers = (spec[k] for k in ("workspace", "bucket", "channels", "readers"))
    if not isinstance(workspace, str) or not WORKSPACE.fullmatch(workspace):
        raise Refusal("invalid workspace")
    if not isinstance(bucket, str) or not BUCKET.fullmatch(bucket) or bucket != "earshot-" + workspace:
        raise Refusal("bucket must be earshot-<workspace> and a valid bucket name")
    if not isinstance(channels, list) or not channels or any(not isinstance(c, str) or not CHANNEL.fullmatch(c) for c in channels) or len(set(channels)) != len(channels):
        raise Refusal("channels must be unique app channel names")
    if not any(channel != "alarm" for channel in channels):
        raise Refusal("at least one non-alarm channel is required by the relay")
    if not isinstance(readers, dict) or not readers:
        raise Refusal("at least one device reader is required")
    for device, grants in readers.items():
        if not isinstance(device, str) or not CHANNEL.fullmatch(device):
            raise Refusal("invalid device name")
        if not isinstance(grants, list) or not grants or any(not isinstance(c, str) for c in grants) or len(set(grants)) != len(grants) or not set(grants) <= set(channels):
            raise Refusal("reader grants must be unique requested channels")
    return {"workspace": workspace, "bucket": bucket, "channels": sorted(channels), "readers": {d: sorted(readers[d]) for d in sorted(readers)}}


def endpoint_url(value):
    if not isinstance(value, str):
        raise Refusal("explicit endpoint required")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise Refusal("invalid endpoint") from None
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None or url.path not in ("", "/") or url.query or url.fragment:
        raise Refusal("endpoint must be an origin without credentials, path or query")
    if port == 0:
        raise Refusal("endpoint port must be between 1 and 65535")
    if url.scheme == "http" and url.hostname not in ("127.0.0.1", "::1"):
        raise Refusal("HTTP is allowed only on explicit loopback IP addresses")
    if url.netloc.endswith(":"):
        raise Refusal("endpoint port must be between 1 and 65535")
    host = url.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{url.scheme}://{host}" + (f":{port}" if port else "")


def protected_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077 or st.st_nlink != 1:
            raise Refusal("secret file must be owned by you, regular, unlinked and mode 0600 or stricter")
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = -1
            return json.load(f)
    finally:
        if fd >= 0:
            os.close(fd)


def private_dir(path):
    path = Path(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise Refusal("state directory must be owned by you and mode 0700 or stricter")
    return path


def rename_noreplace(source, destination):
    try:
        func = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise Refusal("atomic no-replace publication unavailable on this platform") from None
    func.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    func.restype = ctypes.c_int
    if func(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise Refusal("output appeared during publication; refusing to overwrite")
        raise OSError(error, "atomic no-replace publication failed")


def sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def protected_json_temp(directory, prefix, value):
    fd, name = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            st = os.fstat(f.fileno())
        sync_dir(directory)
        return Path(name), st
    except BaseException:
        os.unlink(name)
        raise


def atomic_json(path, value, *, create_only=False):
    name, _ = protected_json_temp(path.parent, ".pending-", value)
    try:
        if create_only:
            rename_noreplace(name, path)
        else:
            os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def expected_handoffs(spec, endpoint, state):
    outputs = {}
    for label, identity in state["identities"].items():
        outputs[f"{label}.json"] = {"version": 1, "provider": "minio", "workspace": spec["workspace"], "endpoint": endpoint, "region": "us-east-1", "bucket": spec["bucket"], "role": "reader" if identity["reader"] else "writer", "device": label[7:] if identity["reader"] else None, "channels": identity["channels"], "access_key": identity["access_key"], "secret_key": identity["secret_key"]}
    return outputs


def validate_input_paths(state_dir, names, input_paths):
    sources = [Path(value).resolve(strict=True) for value in input_paths]
    for name in names:
        destination = state_dir / name
        for source in sources:
            if source == destination.resolve(strict=False):
                raise Refusal("input collides with a reserved state or handoff path")
            if os.path.lexists(destination) and os.path.samefile(source, destination):
                raise Refusal("input aliases a reserved state or handoff path")


def preflight_outputs(state_dir, spec, endpoint, state, input_paths):
    outputs = expected_handoffs(spec, endpoint, state)
    validate_input_paths(state_dir, {"state.json", ".lock", *outputs}, input_paths)
    records = state.get("handoffs")
    if not isinstance(records, dict) or not set(records) <= set(outputs):
        raise Refusal("invalid handoff journal; manual reconciliation required")
    temporary = []
    for name, record in records.items():
        if not isinstance(record, dict) or set(record) != {"temp", "device", "inode"} or not isinstance(record["temp"], str) or not re.fullmatch(r"\.handoff-[a-zA-Z0-9_-]+", record["temp"]) or not isinstance(record["device"], int) or not isinstance(record["inode"], int):
            raise Refusal("invalid handoff provenance; manual reconciliation required")
        temporary.append(state_dir / record["temp"])
    if len(set(temporary)) != len(temporary):
        raise Refusal("duplicate handoff provenance")
    for source in map(lambda x: Path(x).resolve(strict=True), input_paths):
        for temp in temporary:
            if source == temp.resolve(strict=False) or (os.path.lexists(temp) and os.path.samefile(source, temp)):
                raise Refusal("input collides with a journal-owned temporary handoff")
    for name, expected in outputs.items():
        target = state_dir / name
        record = records.get(name)
        if record:
            temp = state_dir / record["temp"]
            if os.path.lexists(target) and os.path.lexists(temp):
                raise Refusal("both temporary and published handoff exist; manual reconciliation required")
            if not os.path.lexists(target) and not os.path.lexists(temp):
                raise Refusal("journal-owned handoff is missing; manual reconciliation required")
        else:
            temp = None
        for path in (target, temp):
            if path is None or not os.path.lexists(path):
                continue
            if record is None:
                raise Refusal("pre-existing handoff is not journal-owned; refusing overwrite")
            try:
                st = path.lstat()
                if (st.st_dev, st.st_ino) != (record["device"], record["inode"]):
                    raise Refusal("handoff provenance mismatch")
                actual = protected_file(path)
            except (OSError, ValueError, Refusal):
                raise Refusal("existing handoff is not a protected journal-owned file") from None
            if actual != expected:
                raise Refusal("existing handoff differs from journal; refusing overwrite")
    return outputs


def publish_handoffs(state_dir, state, outputs, save):
    for name, handoff in outputs.items():
        target = state_dir / name
        record = state["handoffs"].get(name)
        if not record:
            temp, st = protected_json_temp(state_dir, ".handoff-", handoff)
            record = {"temp": temp.name, "device": st.st_dev, "inode": st.st_ino}
            state["handoffs"][name] = record
            save()
        else:
            temp = state_dir / record["temp"]
        if os.path.lexists(target):
            continue
        if not os.path.lexists(temp):
            raise Refusal("journal-owned handoff is missing; manual reconciliation required")
        rename_noreplace(temp, target)
        sync_dir(state_dir)


def policy(bucket, channels, reader):
    objects = [f"arn:aws:s3:::{bucket}/{c}/*" for c in channels]
    statements = [{"Effect": "Allow", "Action": ["s3:GetObject" if reader else "s3:PutObject"], "Resource": objects}]
    if reader:
        statements.append({"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{bucket}"], "Condition": {"StringLike": {"s3:prefix": [f"{c}/*" for c in channels]}}})
    return {"Version": "2012-10-17", "Statement": statements}


def canonical(value):
    if isinstance(value, list):
        return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    return value


def new_state(spec, endpoint):
    def identity(grants, reader):
        return {"access_key": "es" + secrets.token_hex(12), "secret_key": secrets.token_hex(24), "policy": "earshot-" + secrets.token_hex(16), "channels": grants, "reader": reader, "user_attempted": False, "user_confirmed": False, "policy_attempted": False, "policy_confirmed": False, "attachment_confirmed": False}
    return {"version": 2, "spec": spec, "endpoint": endpoint, "binding": secrets.token_hex(32), "bucket_phase": "new", "progress": "initialized", "identities": {"writer": identity(spec["channels"], False), **{f"reader-{device}": identity(grants, True) for device, grants in spec["readers"].items()}}, "handoffs": {}, "complete": False}


class Minio:
    def __init__(self, endpoint, credentials, root):
        from boto3.session import Session
        from botocore.config import Config
        self.session_type = Session
        self.config = Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 0}, connect_timeout=5, read_timeout=20, request_checksum_calculation="when_required", response_checksum_validation="when_required", ignore_configured_endpoint_urls=True)
        self.endpoint = endpoint
        self.s3 = self.client(credentials)
        self.root = root
        self.mcdir = root / "mc"
        self.mcdir.mkdir(mode=0o700)
        self.env = {"PATH": os.defpath + ":/usr/local/bin", "HOME": str(root), "LANG": "C", "NO_PROXY": "*"}
        self.mc(["alias", "import", "setup"], json.dumps({"url": endpoint, "accessKey": credentials["access_key"], "secretKey": credentials["secret_key"], "api": "s3v4", "path": "on"}))

    def client(self, credentials):
        return self.session_type().client("s3", endpoint_url=self.endpoint, region_name="us-east-1", aws_access_key_id=credentials["access_key"], aws_secret_access_key=credentials["secret_key"], aws_session_token=None, config=self.config, verify=True)

    def mc(self, args, data=None, missing=None):
        cmd = ["mc", "--config-dir", str(self.mcdir), "--json", *args]
        result = subprocess.run(cmd, input=data, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=30)
        try:
            obj = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise Refusal("provider command returned unreadable output") from None
        if result.returncode:
            code = obj.get("error", {}).get("cause", {}).get("error", {}).get("Code")
            if code == missing and missing is not None:
                return None
            raise Refusal("provider command failed; inspect provider state before retrying")
        if obj.get("status") != "success":
            raise Refusal("provider command did not confirm success")
        return obj

    def user_info(self, access_key):
        return self.mc(["admin", "user", "info", "setup", access_key], missing="XMinioAdminNoSuchUser")

    def policy_info(self, name):
        result = self.mc(["admin", "policy", "info", "setup", name], missing="XMinioAdminNoSuchPolicy")
        return None if result is None else result.get("policyInfo", {}).get("Policy")

    def create_policy(self, name, document):
        file = self.root / "policy.json"
        atomic_json(file, document)
        self.mc(["admin", "policy", "create", "setup", name, str(file)])

    def assert_no_service_accounts(self, access_key):
        cmd = ["mc", "--config-dir", str(self.mcdir), "--json", "admin", "user", "svcacct", "ls", "setup", access_key]
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=30)
        if result.returncode != 0:
            raise Refusal("could not verify provider service accounts")
        if result.stdout.strip():
            raise Refusal("provider user has service accounts; refusing setup")

    def add_user(self, identity):
        self.mc(["admin", "user", "add", "setup"], identity["access_key"] + "\n" + identity["secret_key"] + "\n")

    def attach(self, identity):
        self.mc(["admin", "policy", "attach", "setup", identity["policy"], "--user", identity["access_key"]])


def bucket_creation_time(admin, bucket):
    matches = [item for item in admin.s3.list_buckets().get("Buckets", []) if item.get("Name") == bucket]
    if len(matches) != 1 or not matches[0].get("CreationDate"):
        raise Refusal("cannot verify bucket creation time; inspect provider state")
    return matches[0]["CreationDate"].isoformat()


def require_bucket_continuity(admin, bucket, state, save):
    try:
        observed = state.get("bucket_created_at")
        if not observed or bucket_creation_time(admin, bucket) != observed:
            raise Refusal("bucket creation time missing or changed; continuity unknown")
    except Refusal:
        state["progress"] = "bucket continuity unknown"
        save()
        raise


def fresh_bucket_contents(admin, bucket, expected_keys):
    versioning = admin.s3.get_bucket_versioning(Bucket=bucket)
    if versioning.get("Status") or versioning.get("MFADelete"):
        return False
    response = admin.s3.list_objects_v2(Bucket=bucket, MaxKeys=2)
    if response.get("IsTruncated") or sorted(item["Key"] for item in response.get("Contents", [])) != expected_keys:
        return False
    versions = admin.s3.list_object_versions(Bucket=bucket, MaxKeys=2)
    version_items = versions.get("Versions", [])
    if versions.get("IsTruncated") or versions.get("DeleteMarkers") or sorted(item["Key"] for item in version_items) != expected_keys:
        return False
    if any(not item.get("IsLatest") or item.get("VersionId") != "null" for item in version_items):
        return False
    uploads = admin.s3.list_multipart_uploads(Bucket=bucket, MaxUploads=1)
    return not uploads.get("Uploads") and not uploads.get("IsTruncated")


def fresh_bucket_is_empty(admin, bucket):
    return fresh_bucket_contents(admin, bucket, [])


def fresh_bucket_has_only_marker(admin, bucket, marker_key):
    return fresh_bucket_contents(admin, bucket, [marker_key])


def error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def apply(spec, endpoint, credentials, state_dir, input_paths):
    # Python's TLS contexts honor this inherited variable even with certificate verification enabled.
    os.environ.pop("SSLKEYLOGFILE", None)
    from botocore.exceptions import ClientError
    state_dir = Path(state_dir)
    path = state_dir / "state.json"
    reserved = {"state.json", ".lock", "writer.json", *(f"reader-{device}.json" for device in spec["readers"])}
    validate_input_paths(state_dir, reserved, input_paths)
    state_dir = private_dir(state_dir)
    lock_fd = os.open(state_dir / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    journal_ready = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(path):
            state = protected_file(path)
            if state.get("version") != 2 or state.get("spec") != spec or state.get("endpoint") != endpoint:
                raise Refusal("state workspace, spec, endpoint or version mismatch; no changes made")
            preflight_outputs(state_dir, spec, endpoint, state, input_paths)
        else:
            state = new_state(spec, endpoint)
            preflight_outputs(state_dir, spec, endpoint, state, input_paths)
            atomic_json(path, state, create_only=True)
        journal_ready = True
        def save():
            atomic_json(path, state)
        state["complete"] = False
        state["progress"] = "checking provider"
        save()
        with tempfile.TemporaryDirectory(prefix=".provider-", dir=state_dir) as temporary:
            os.chmod(temporary, 0o700)
            for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "MC_HOST_setup", "MC_CONFIG_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                os.environ.pop(key, None)
            os.environ.update({"HOME": str(temporary), "AWS_CONFIG_FILE": str(Path(temporary) / "no-aws-config"), "AWS_SHARED_CREDENTIALS_FILE": str(Path(temporary) / "no-aws-credentials"), "AWS_EC2_METADATA_DISABLED": "true", "NO_PROXY": "*"})
            admin = Minio(endpoint, credentials, Path(temporary))
            bucket = spec["bucket"]
            marker_key = ".earshot-provisioner/binding.json"
            marker = {"version": 1, "workspace": spec["workspace"], "binding": state["binding"]}
            created_this_run = False
            if state["bucket_phase"] == "new":
                try:
                    admin.s3.head_bucket(Bucket=bucket)
                except ClientError as exc:
                    if error_code(exc) not in ("404", "NoSuchBucket", "NotFound"):
                        raise Refusal("cannot verify bucket absence; no bucket created") from None
                else:
                    raise Refusal("bucket already exists; automatic adoption is forbidden")
                for identity in state["identities"].values():
                    if admin.user_info(identity["access_key"]) is not None or admin.policy_info(identity["policy"]) is not None:
                        raise Refusal("generated identity already exists; refusing collision")
                state["bucket_phase"] = "creating"
                state["progress"] = "bucket creation attempted; outcome may be unknown"
                save()
                admin.s3.create_bucket(Bucket=bucket)
                created_this_run = True
                state["bucket_created_at"] = bucket_creation_time(admin, bucket)
                state["bucket_phase"] = "created"
                state["progress"] = "bucket created; binding pending"
                save()
            if state["bucket_phase"] == "creating":
                state["progress"] = "bucket creation outcome unknown"
                save()
                raise Refusal("bucket creation outcome unknown; inspect provider and state, do not retry blindly")
            if state["bucket_phase"] == "created":
                if not created_this_run:
                    state["progress"] = "bucket continuity unknown"
                    save()
                    raise Refusal("unbound bucket continuity unknown; no binding or IAM changes made")
                require_bucket_continuity(admin, bucket, state, save)
                if not fresh_bucket_is_empty(admin, bucket):
                    state["progress"] = "fresh bucket is not empty"
                    save()
                    raise Refusal("fresh bucket is not empty; refusing binding")
                require_bucket_continuity(admin, bucket, state, save)
                try:
                    admin.s3.put_object(Bucket=bucket, Key=marker_key, Body=json.dumps(marker).encode(), IfNoneMatch="*")
                except ClientError as exc:
                    if error_code(exc) not in ("PreconditionFailed", "412"):
                        raise
            require_bucket_continuity(admin, bucket, state, save)
            obj = admin.s3.get_object(Bucket=bucket, Key=marker_key)
            if json.loads(obj["Body"].read()) != marker:
                raise Refusal("bucket binding mismatch; refusing provider changes")
            if state["bucket_phase"] == "created":
                state["bucket_phase"] = "bound"
                state["fresh_bucket_guard"] = True
                state["progress"] = "bucket binding confirmed"
                save()
            try:
                admin.s3.get_bucket_policy(Bucket=bucket)
            except ClientError as exc:
                if error_code(exc) != "NoSuchBucketPolicy":
                    raise Refusal("cannot verify anonymous bucket policy") from None
            else:
                raise Refusal("bucket has a policy; private-only setup refuses to proceed")
            if state.get("fresh_bucket_guard") and not fresh_bucket_has_only_marker(admin, bucket, marker_key):
                state["progress"] = "fresh bucket contains unexpected objects"
                save()
                raise Refusal("fresh bucket contains unexpected objects; refusing identity issuance")
            for label, identity in sorted(state["identities"].items(), key=lambda pair: pair[0] == "writer"):
                require_bucket_continuity(admin, bucket, state, save)
                obj = admin.s3.get_object(Bucket=bucket, Key=marker_key)
                if json.loads(obj["Body"].read()) != marker:
                    raise Refusal("bucket binding mismatch; refusing identity issuance")
                if state.get("fresh_bucket_guard") and not fresh_bucket_has_only_marker(admin, bucket, marker_key):
                    state["progress"] = "fresh bucket contains unexpected objects"
                    save()
                    raise Refusal("fresh bucket contains unexpected objects; refusing identity issuance")
                state["progress"] = "checking " + label
                save()
                expected = policy(bucket, identity["channels"], identity["reader"])
                existing = admin.policy_info(identity["policy"])
                if existing is None:
                    if identity["policy_confirmed"]:
                        raise Refusal("confirmed policy is missing; refusing to recreate it")
                    identity["policy_attempted"] = True
                    state["progress"] = "creating policy for " + label
                    save()
                    admin.create_policy(identity["policy"], expected)
                    existing = admin.policy_info(identity["policy"])
                elif not identity["policy_attempted"]:
                    raise Refusal("unexpected pre-existing policy")
                if canonical(existing) != canonical(expected):
                    raise Refusal("provider policy differs from expected; refusing to overwrite")
                identity["policy_confirmed"] = True
                save()
                info = admin.user_info(identity["access_key"])
                if info is None:
                    if identity["user_confirmed"]:
                        raise Refusal("confirmed user is missing; refusing to recreate it")
                    if identity["user_attempted"]:
                        state["progress"] = "attempted user missing for " + label
                        save()
                        raise Refusal("attempted user is missing; outcome or revocation unknown; refusing recreation")
                    require_bucket_continuity(admin, bucket, state, save)
                    obj = admin.s3.get_object(Bucket=bucket, Key=marker_key)
                    if json.loads(obj["Body"].read()) != marker:
                        raise Refusal("bucket binding mismatch; refusing identity issuance")
                    if state.get("fresh_bucket_guard") and not fresh_bucket_has_only_marker(admin, bucket, marker_key):
                        state["progress"] = "fresh bucket contains unexpected objects"
                        save()
                        raise Refusal("fresh bucket contains unexpected objects; refusing identity issuance")
                    identity["user_attempted"] = True
                    state["progress"] = "creating user for " + label
                    save()
                    admin.add_user(identity)
                    info = admin.user_info(identity["access_key"])
                elif not identity["user_attempted"]:
                    raise Refusal("unexpected pre-existing user")
                elif not identity["user_confirmed"]:
                    state["progress"] = "attempted user unconfirmed for " + label
                    save()
                    raise Refusal("attempted user remains unconfirmed; manual reconciliation required")
                if not info or info.get("userStatus") != "enabled" or info.get("memberOf"):
                    raise Refusal("provider user disabled, missing or in a group")
                admin.assert_no_service_accounts(identity["access_key"])
                policies = set(filter(None, info.get("policyName", "").split(",")))
                if policies - {identity["policy"]}:
                    raise Refusal("provider user has unexpected policies")
                if not policies:
                    if identity["attachment_confirmed"] or identity["user_confirmed"]:
                        raise Refusal("confirmed policy attachment is missing; refusing to reattach")
                    if not identity["reader"] and state.get("fresh_bucket_guard"):
                        require_bucket_continuity(admin, bucket, state, save)
                        obj = admin.s3.get_object(Bucket=bucket, Key=marker_key)
                        if json.loads(obj["Body"].read()) != marker:
                            raise Refusal("bucket binding mismatch; refusing identity issuance")
                        if not fresh_bucket_has_only_marker(admin, bucket, marker_key):
                            state["progress"] = "fresh bucket contains unexpected objects"
                            save()
                            raise Refusal("fresh bucket contains unexpected objects; refusing identity issuance")
                        state["fresh_bucket_guard"] = False
                        save()
                    try:
                        admin.client(identity).list_objects_v2(Bucket=bucket, Prefix=identity["channels"][0] + "/", MaxKeys=1)
                    except ClientError as exc:
                        if error_code(exc) != "AccessDenied":
                            raise Refusal("user credentials do not authenticate; refusing attachment") from None
                    else:
                        raise Refusal("user has unexpected effective permissions; refusing attachment")
                    state["progress"] = "attaching policy for " + label
                    save()
                    admin.attach(identity)
                    info = admin.user_info(identity["access_key"])
                    if not info or info.get("policyName") != identity["policy"]:
                        raise Refusal("provider did not confirm policy attachment")
                identity["user_confirmed"] = True
                identity["attachment_confirmed"] = True
                save()
                client = admin.client(identity)
                try:
                    client.list_objects_v2(Bucket=bucket, Prefix=identity["channels"][0] + "/", MaxKeys=1)
                except ClientError as exc:
                    if identity["reader"] or error_code(exc) != "AccessDenied":
                        raise Refusal("identity authentication or policy verification failed") from None
                else:
                    if not identity["reader"]:
                        raise Refusal("writer unexpectedly has list permission")
            state["progress"] = "handoff files pending"
            save()
            outputs = preflight_outputs(state_dir, spec, endpoint, state, input_paths)
            publish_handoffs(state_dir, state, outputs, save)
            preflight_outputs(state_dir, spec, endpoint, state, input_paths)
            state["progress"] = "complete"
            state["complete"] = True
            save()
    except KeyboardInterrupt:
        if journal_ready and os.path.lexists(path):
            state = protected_file(path)
            state["complete"] = False
            state["progress"] = "interrupted: " + state.get("progress", "unknown")
            atomic_json(path, state)
        raise Refusal("interrupted; inspect protected state and provider") from None
    finally:
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plan by default; --apply provisions one new MinIO workspace bucket")
    parser.add_argument("--spec", required=True, help="JSON workspace specification")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--endpoint")
    parser.add_argument("--admin-file", help="0600 JSON file containing access_key and secret_key")
    parser.add_argument("--state-dir", help="private directory for journal and handoffs")
    args = parser.parse_args(argv)
    try:
        with open(args.spec, encoding="utf-8") as f:
            spec = validate_spec(json.load(f))
        if not args.apply:
            print(json.dumps({"mode": "plan", "provider": "minio", "spec": spec, "side_effects": False}, sort_keys=True))
            return 0
        if not args.endpoint or not args.admin_file or not args.state_dir:
            raise Refusal("apply requires --endpoint, --admin-file and --state-dir")
        endpoint = endpoint_url(args.endpoint)
        credentials = protected_file(args.admin_file)
        if not isinstance(credentials, dict) or set(credentials) != {"access_key", "secret_key"} or any(not isinstance(credentials[k], str) or not credentials[k] or "\n" in credentials[k] or "\r" in credentials[k] for k in credentials):
            raise Refusal("admin file must contain nonempty access_key and secret_key strings")
        apply(spec, endpoint, credentials, args.state_dir, [args.spec, args.admin_file])
        print("apply complete; protected handoffs and recovery state are in the state directory")
        return 0
    except (Refusal, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Never print provider output, input values or exception messages from filesystem/network libraries.
        message = str(exc) if isinstance(exc, Refusal) else "local or provider operation failed; inspect protected state and provider"
        print("not complete: " + message, file=sys.stderr)
        return 1
    except Exception as exc:
        print("not complete: provider operation failed (" + type(exc).__name__ + "); inspect protected state and provider", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
