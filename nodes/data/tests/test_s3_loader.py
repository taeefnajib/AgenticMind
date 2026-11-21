"""S3 Loader (ADR-0026): listing, filtering, parsing, limits and errors against botocore's Stubber."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import boto3
import pytest
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_data import s3 as s3_module
from agenticmind_nodes_data.nodes import S3LoaderConfig, s3_loader
from botocore.response import StreamingBody
from botocore.stub import Stubber

KEYS = {"access_key_id": {"$secret": "AK"}, "secret_access_key": {"$secret": "SK"}}
SECRETS = {"AK": "AKIAFIXTURE", "SK": "fixture-secret"}
WHEN = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)


def body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


@pytest.fixture
def stubbed(monkeypatch):
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    stub = Stubber(client)
    made = []

    def fake_make_client(**kwargs):
        made.append(kwargs)
        return client

    monkeypatch.setattr(s3_module, "make_client", fake_make_client)
    with stub:
        yield stub, made


def obj(key, data):
    return {"Body": body(data), "ContentLength": len(data), "ETag": '"abc"', "LastModified": WHEN}


async def test_lists_filters_and_parses_objects(stubbed):
    stub, made = stubbed
    stub.add_response(
        "list_objects_v2",
        {"IsTruncated": True, "NextContinuationToken": "t2", "KeyCount": 3, "Contents": [
            {"Key": "docs/", "Size": 0}, {"Key": "docs/handbook.md", "Size": 20, "ETag": '"e1"', "LastModified": WHEN},
            {"Key": "docs/logo.svg", "Size": 9}]},
        {"Bucket": "acme-kb", "Prefix": "docs/", "MaxKeys": 1000},
    )  # fmt: skip
    stub.add_response(
        "list_objects_v2",
        {"IsTruncated": False, "KeyCount": 1, "Contents": [{"Key": "docs/data/people.csv", "Size": 30}]},
        {"Bucket": "acme-kb", "Prefix": "docs/", "MaxKeys": 1000, "ContinuationToken": "t2"},
    )
    stub.add_response("get_object", obj("docs/handbook.md", b"# Leave\n25 days a year."),
                      {"Bucket": "acme-kb", "Key": "docs/handbook.md"})  # fmt: skip
    stub.add_response("get_object", obj("docs/data/people.csv", b"name,team\nAda,Research\n"),
                      {"Bucket": "acme-kb", "Key": "docs/data/people.csv"})  # fmt: skip
    r = await invoke_node(s3_loader, config={**KEYS, "bucket": "acme-kb", "prefix": "docs/", "region": "eu-west-1"},
                          secrets=SECRETS)  # fmt: skip
    docs = r.outputs["documents"]
    assert [d["source"]["uri"] for d in docs] == ["s3://acme-kb/docs/handbook.md", "s3://acme-kb/docs/data/people.csv"]
    assert docs[0]["content"].startswith("# Leave") and docs[1]["content"] == "name: Ada\nteam: Research"
    assert docs[0]["metadata"]["etag"] == "abc" and docs[0]["metadata"]["last_modified"].startswith("2026-11-20")
    assert [o["key"] for o in r.outputs["objects"]] == ["docs/handbook.md", "docs/data/people.csv"]
    assert made[0]["access_key_id"] == "AKIAFIXTURE" and made[0]["region"] == "eu-west-1" and made[0]["auth"] == "keys"
    stub.assert_no_pending_responses()


async def test_explicit_keys_patterns_and_limits(stubbed):
    stub, _ = stubbed
    stub.add_response("get_object", obj("a.txt", b"alpha"), {"Bucket": "b1", "Key": "a.txt"})
    r = await invoke_node(s3_loader, config={**KEYS, "bucket": "b1", "keys": ["a.txt"]}, secrets=SECRETS)
    assert r.outputs["documents"][0]["content"] == "alpha"

    stub.add_response("list_objects_v2", {"IsTruncated": False, "Contents": [
        {"Key": "r/1.pdf", "Size": 1}, {"Key": "r/2.md", "Size": 1}, {"Key": "r/3.md", "Size": 1}]})  # fmt: skip
    stub.add_response("get_object", obj("r/2.md", b"two"), {"Bucket": "b1", "Key": "r/2.md"})
    r = await invoke_node(s3_loader, config={**KEYS, "bucket": "b1", "include": ["*.md"], "max_files": 1},
                          secrets=SECRETS)  # fmt: skip
    assert [d["content"] for d in r.outputs["documents"]] == ["two"]

    big = b"x" * (2 * 2**20)
    stub.add_response("get_object", {"Body": body(big), "ContentLength": len(big)}, {"Bucket": "b1", "Key": "big.txt"})
    with pytest.raises(NodeError) as exc:
        await invoke_node(s3_loader, config={**KEYS, "bucket": "b1", "keys": ["big.txt"], "max_file_mb": 1},
                          secrets=SECRETS)  # fmt: skip
    assert exc.value.code == "S3_OBJECT_TOO_LARGE"


async def test_errors_are_actionable(stubbed):
    stub, _ = stubbed
    stub.add_client_error("list_objects_v2", service_error_code="AccessDenied", http_status_code=403)
    with pytest.raises(NodeError) as exc:
        await invoke_node(s3_loader, config={**KEYS, "bucket": "locked"}, secrets=SECRETS)
    assert exc.value.code == "S3_ACCESS_DENIED" and "locked" in str(exc.value)
    stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    with pytest.raises(NodeError) as exc:
        await invoke_node(s3_loader, config={**KEYS, "bucket": "bkt", "keys": ["gone.pdf"]}, secrets=SECRETS)
    assert exc.value.code == "S3_NOT_FOUND" and "s3://bkt/gone.pdf" in str(exc.value)
    stub.add_response("list_objects_v2", {"IsTruncated": False, "Contents": [{"Key": "x.bin", "Size": 1}]})
    with pytest.raises(NodeError) as exc:
        await invoke_node(s3_loader, config={**KEYS, "bucket": "bkt"}, secrets=SECRETS)
    assert exc.value.code == "NO_FILES" and "skipped 1" in str(exc.value)


def test_config_and_endpoint_safety(monkeypatch):
    with pytest.raises(ValueError, match="access_key_id and secret_access_key"):
        S3LoaderConfig(bucket="bkt")
    assert S3LoaderConfig(bucket="public-data", auth="anonymous").auth == "anonymous"
    with pytest.raises(ValueError):
        S3LoaderConfig(bucket="s3://bad", auth="anonymous")
    for url in ("http://169.254.169.254", "http://metadata.google.internal", "ftp://minio"):
        with pytest.raises(NodeError) as exc:
            s3_module.check_endpoint(url)
        assert exc.value.code in ("S3_ENDPOINT_BLOCKED", "S3_CONFIG_INVALID")
    s3_module.check_endpoint("http://127.0.0.1:9000")  # self-hosted MinIO stays allowed


def test_real_client_options_for_compatible_storage():
    r2 = "https://acct.r2.cloudflarestorage.com"
    client = s3_module.make_client(region="auto", endpoint_url=r2, auth="keys", access_key_id="k",
                                   secret_access_key="s", session_token=None, timeout=30)  # fmt: skip
    assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert client.meta.config.s3["addressing_style"] == "path"
    anon = s3_module.make_client(region="us-east-1", endpoint_url=None, auth="anonymous", access_key_id=None,
                                 secret_access_key=None, session_token=None, timeout=30)  # fmt: skip
    from botocore import UNSIGNED

    assert anon.meta.config.signature_version is UNSIGNED
