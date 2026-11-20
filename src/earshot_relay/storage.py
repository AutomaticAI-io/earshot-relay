"""The S3 writer. It only ever calls PutObject: the relay never lists, reads or deletes the bucket."""

from __future__ import annotations

from typing import IO

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError

from .config import Settings

# Short, credential-free hints for the errors buyers actually hit.
HINTS = {
    "AccessDenied": "the writer key may not write this channel's prefix (it needs s3:PutObject on <bucket>/<channel>/*)",
    "AllAccessDisabled": "the writer key may not write this bucket",
    "NoSuchBucket": "the bucket named in EARSHOT_BUCKET does not exist at EARSHOT_S3_ENDPOINT",
    "SignatureDoesNotMatch": "the writer secret is wrong, or EARSHOT_REGION does not match the provider's signing region",
    "InvalidAccessKeyId": "the writer access key id is unknown to the provider",
    "AuthorizationHeaderMalformed": "EARSHOT_REGION does not match the provider's signing region",
    "InvalidArgument": "the provider rejected the request; check EARSHOT_REGION and the endpoint",
    "RequestTimeTooSkewed": "the relay host clock is too far off; sync it (NTP)",
    "EntityTooLarge": "the provider refused the object size",
}


class StorageError(Exception):
    """PutObject failed. `code` is the provider's error code (no credentials or endpoint secrets)."""

    def __init__(self, code: str, hint: str):
        super().__init__(f"{code}: {hint}")
        self.code = code
        self.hint = hint


def s3_config() -> Config:
    # when_required: boto3 >= 1.36 adds CRC32 checksums by default, which several S3-compatible stores reject.
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=5,
        read_timeout=30,
    )


class S3Writer:
    def __init__(self, settings: Settings, client=None):
        self.bucket = settings.bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.region,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            config=s3_config(),
        )

    def put(self, key: str, body: bytes | IO[bytes], content_type: str) -> None:
        """Blocking PutObject. Run it in a worker thread from async code."""
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        except ClientError as exc:
            err = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
            code = str(err.get("Code") or "ClientError")[:64]
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(exc.response, dict) else None
            hint = HINTS.get(code) or f"the provider refused the write (HTTP {status or '?'})"
            raise StorageError(code, hint) from None
        except (EndpointConnectionError, ConnectTimeoutError):
            raise StorageError("EndpointUnreachable", "the S3 endpoint could not be reached") from None
        except ReadTimeoutError:
            raise StorageError("Timeout", "the S3 endpoint did not answer in time") from None
        except BotoCoreError as exc:
            raise StorageError(type(exc).__name__, "the S3 client failed before the provider answered") from None
