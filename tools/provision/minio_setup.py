#!/usr/bin/env python3
"""Standalone, deliberately narrow MinIO workspace provisioner; see README.md."""
from __future__ import annotations

import argparse
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


def atomic_json(path, value):
    directory = path.parent
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


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
    return {"version": 1, "spec": spec, "endpoint": endpoint, "binding": secrets.token_hex(32), "bucket_phase": "new", "progress": "initialized", "identities": {"writer": identity(spec["channels"], False), **{f"reader-{device}": identity(grants, True) for device, grants in spec["readers"].items()}}, "complete": False}


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


def error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def apply(spec, endpoint, credentials, state_dir):
    # Python's TLS contexts honor this inherited variable even with certificate verification enabled.
    os.environ.pop("SSLKEYLOGFILE", None)
    from botocore.exceptions import ClientError
    state_dir = private_dir(state_dir)
    path = state_dir / "state.json"
    lock_fd = os.open(state_dir / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(path):
            state = protected_file(path)
            if state.get("version") != 1 or state.get("spec") != spec or state.get("endpoint") != endpoint:
                raise Refusal("state workspace, spec, endpoint or version mismatch; no changes made")
        else:
            state = new_state(spec, endpoint)
            atomic_json(path, state)
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
                try:
                    admin.s3.put_object(Bucket=bucket, Key=marker_key, Body=json.dumps(marker).encode(), IfNoneMatch="*")
                except ClientError as exc:
                    if error_code(exc) not in ("PreconditionFailed", "412"):
                        raise
            obj = admin.s3.get_object(Bucket=bucket, Key=marker_key)
            if json.loads(obj["Body"].read()) != marker:
                raise Refusal("bucket binding mismatch; refusing provider changes")
            if state["bucket_phase"] == "created":
                state["bucket_phase"] = "bound"
                state["progress"] = "bucket binding confirmed"
                save()
            try:
                admin.s3.get_bucket_policy(Bucket=bucket)
            except ClientError as exc:
                if error_code(exc) != "NoSuchBucketPolicy":
                    raise Refusal("cannot verify anonymous bucket policy") from None
            else:
                raise Refusal("bucket has a policy; private-only setup refuses to proceed")
            for label, identity in state["identities"].items():
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
            for label, identity in state["identities"].items():
                handoff = {"version": 1, "provider": "minio", "workspace": spec["workspace"], "endpoint": endpoint, "region": "us-east-1", "bucket": bucket, "role": "reader" if identity["reader"] else "writer", "device": label[7:] if identity["reader"] else None, "channels": identity["channels"], "access_key": identity["access_key"], "secret_key": identity["secret_key"]}
                atomic_json(state_dir / f"{label}.json", handoff)
            state["progress"] = "complete"
            state["complete"] = True
            save()
    except KeyboardInterrupt:
        if os.path.lexists(path):
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
        apply(spec, endpoint, credentials, args.state_dir)
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
