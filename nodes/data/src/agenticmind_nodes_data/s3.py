"""Amazon S3 and S3-compatible object storage (ADR-0026), through boto3 (``agenticmind-nodes-data[s3]``).

S3-compatible services (MinIO, Cloudflare R2, Wasabi, Backblaze B2, Ceph, Google Cloud Storage interop) are
reached with ``endpoint_url`` and path-style addressing. Endpoints resolving to link-local addresses (cloud
metadata services such as 169.254.169.254) are refused, so an endpoint cannot be used to read the server's
own cloud credentials; localhost and private networks stay allowed for self-hosted MinIO.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from agenticmind_node_sdk import NodeError, TransientNodeError

BLOCKED_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    etag: str = ""
    last_modified: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "size": self.size, "etag": self.etag, "last_modified": self.last_modified}


def check_endpoint(endpoint_url: str) -> None:
    """Refuse endpoints that point at link-local / metadata addresses (SSRF to cloud credentials)."""
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise NodeError("S3_CONFIG_INVALID", "endpoint_url must be an http(s) URL")
    host = parsed.hostname.lower()
    if host in BLOCKED_HOSTS:
        raise NodeError("S3_ENDPOINT_BLOCKED", f"{host} is a cloud metadata endpoint")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise TransientNodeError("S3_UNAVAILABLE", f"Cannot resolve {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or str(ip) == "fd00:ec2::254":
            raise NodeError("S3_ENDPOINT_BLOCKED", f"{host} resolves to {ip}, a link-local/metadata address")


def make_client(
    *,
    region: str | None,
    endpoint_url: str | None,
    auth: str,
    access_key_id: str | None,
    secret_access_key: str | None,
    session_token: str | None,
    timeout: float,
) -> Any:
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise NodeError("S3_DRIVER_MISSING", "S3 needs boto3: pip install 'agenticmind-nodes-data[s3]'") from exc
    options: dict[str, Any] = {
        "connect_timeout": 10,
        "read_timeout": timeout,
        "retries": {"max_attempts": 3, "mode": "standard"},
        "user_agent_extra": "AgenticMind",
    }
    if endpoint_url:
        options["s3"] = {"addressing_style": "path"}  # MinIO and most compatible services
    if auth == "anonymous":
        options["signature_version"] = UNSIGNED
    kwargs: dict[str, Any] = {"config": Config(**options)}
    if region:
        kwargs["region_name"] = region
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if auth == "keys":
        kwargs.update(
            aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key, aws_session_token=session_token
        )
    session = boto3.session.Session()
    return session.client("s3", **kwargs)


def _client_error(exc: Exception, bucket: str, key: str | None = None) -> NodeError:
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
    where = f"s3://{bucket}/{key}" if key else f"bucket '{bucket}'"
    if code in ("NoSuchBucket",):
        return NodeError("S3_NOT_FOUND", f"The bucket '{bucket}' does not exist")
    if code in ("NoSuchKey", "404", "NotFound"):
        return NodeError("S3_NOT_FOUND", f"{where} does not exist")
    if code in ("AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch", "AllAccessDisabled"):
        return NodeError("S3_ACCESS_DENIED", f"Access denied to {where} ({code}); check the credentials and policy")
    if code in ("SlowDown", "ServiceUnavailable", "InternalError", "RequestTimeout"):
        return TransientNodeError("S3_UNAVAILABLE", f"S3 is busy ({code})")
    name = type(exc).__name__
    if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError", "ConnectionClosedError"):
        return TransientNodeError("S3_UNAVAILABLE", f"Could not reach S3: {exc}")
    if name == "NoCredentialsError":
        return NodeError("S3_NO_CREDENTIALS", "No AWS credentials found on the server (use access key secrets)")
    return NodeError("S3_ERROR", f"{where}: {code or name}: {exc}")


def _matches(key: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    name = key.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name.lower(), p.lower()) or fnmatch.fnmatch(key.lower(), p.lower()) for p in patterns)


def list_objects(client: Any, bucket: str, prefix: str, patterns: list[str], max_files: int) -> list[S3Object]:
    """Objects under ``prefix`` matching ``patterns`` (on the file name or full key), up to ``max_files``."""
    out: list[S3Object] = []
    token: str | None = None
    try:
        while len(out) < max_files:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                key = item["Key"]
                if key.endswith("/") or not _matches(key, patterns):
                    continue
                modified = item.get("LastModified")
                out.append(
                    S3Object(
                        key=key,
                        size=int(item.get("Size", 0)),
                        etag=str(item.get("ETag", "")).strip('"'),
                        last_modified=modified.isoformat() if hasattr(modified, "isoformat") else str(modified or ""),
                    )
                )
                if len(out) >= max_files:
                    break
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    except NodeError:
        raise
    except Exception as exc:  # noqa: BLE001 - botocore ClientError and friends
        raise _client_error(exc, bucket) from exc
    return out


def get_object(client: Any, bucket: str, key: str, max_bytes: int) -> tuple[bytes, S3Object]:
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        size = int(resp.get("ContentLength", 0))
        if size > max_bytes:
            resp["Body"].close()
            raise NodeError(
                "S3_OBJECT_TOO_LARGE", f"s3://{bucket}/{key} is {size // 2**20} MB (limit {max_bytes // 2**20} MB)"
            )
        data = resp["Body"].read(max_bytes + 1)
    except NodeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _client_error(exc, bucket, key) from exc
    if len(data) > max_bytes:
        raise NodeError("S3_OBJECT_TOO_LARGE", f"s3://{bucket}/{key} exceeds {max_bytes // 2**20} MB")
    modified = resp.get("LastModified")
    return data, S3Object(
        key=key,
        size=len(data),
        etag=str(resp.get("ETag", "")).strip('"'),
        last_modified=modified.isoformat() if hasattr(modified, "isoformat") else str(modified or ""),
    )
