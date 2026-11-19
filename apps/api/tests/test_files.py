import time

import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_api.templates import load_template
from agenticmind_nodes_data.testing import make_test_pdf
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        max_upload_bytes=200_000,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def wait_for_run(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("succeeded", "failed", "cancelled"):
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def upload(client, pid, name, data):
    return client.post(f"/api/projects/{pid}/files", files={"file": (name, data, "application/octet-stream")})


def test_upload_list_download_delete(client):
    pid = client.post("/api/projects", json={"name": "Files"}).json()["id"]
    resp = upload(client, pid, "../../handbook.md", b"# Handbook\nhello")
    assert resp.status_code == 201
    info = resp.json()
    assert info["filename"] == "handbook.md" and info["media_type"] == "text/markdown" and info["size"] == 16

    assert [f["id"] for f in client.get(f"/api/projects/{pid}/files").json()] == [info["id"]]
    dl = client.get(f"/api/projects/{pid}/files/{info['id']}/content")
    assert dl.content == b"# Handbook\nhello"
    assert dl.headers["content-disposition"].startswith("attachment")
    assert dl.headers["x-content-type-options"] == "nosniff"

    other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    assert client.get(f"/api/projects/{other}/files/{info['id']}").status_code == 404  # project-scoped

    assert client.delete(f"/api/projects/{pid}/files/{info['id']}").status_code == 204
    assert client.get(f"/api/projects/{pid}/files").json() == []


@pytest.mark.parametrize(
    "name,data,status,code",
    [
        ("virus.exe", b"MZ", 415, "ARTIFACT_TYPE_NOT_ALLOWED"),
        ("empty.txt", b"", 400, "ARTIFACT_EMPTY"),
        ("big.txt", b"x" * 300_000, 413, "ARTIFACT_TOO_LARGE"),
    ],
)
def test_upload_rejections(client, name, data, status, code):
    pid = client.post("/api/projects", json={"name": "Files"}).json()["id"]
    resp = upload(client, pid, name, data)
    assert resp.status_code == status and resp.json()["detail"]["code"] == code


def test_ingestion_template_end_to_end(client):
    pid = client.post("/api/projects", json={"name": "Ingest", "template": "document-ingestion"}).json()["id"]
    pdf = upload(
        client, pid, "policy.pdf", make_test_pdf(["# Refunds\nRefunds within 30 days.", "Shipping takes 2 days."])
    )
    md = upload(client, pid, "faq.md", b"# FAQ\n\n## Hours\nOpen 9-5.\n\n## Contact\nEmail us.")
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    loader = next(n for n in doc["nodes"] if n["id"] == "load")
    loader["config"]["files"] = [pdf.json()["id"], md.json()["id"]]
    client.put(f"/api/projects/{pid}", json={"document": doc})

    run_id = client.post(f"/api/projects/{pid}/runs", json={}).json()["id"]
    final = wait_for_run(client, run_id)
    assert final["status"] == "succeeded", final["error"]
    chunks = final["outputs"]["chunks"]
    sections = [c["source"].get("section") for c in chunks if c["source"]["filename"] == "faq.md"]
    assert sections == ["FAQ", "FAQ > Hours", "FAQ > Contact"]
    pages = {c["source"].get("page") for c in chunks if c["source"]["filename"] == "policy.pdf"}
    assert pages == {1, 2}
    loaded = client.get(f"/api/runs/{run_id}/nodes/load/outputs").json()["outputs"]["documents"]
    assert len(loaded) == 3


def test_run_without_files_reports_node_error(client):
    pid = client.post("/api/projects", json={"name": "Ingest", "template": "document-ingestion"}).json()["id"]
    final = wait_for_run(client, client.post(f"/api/projects/{pid}/runs", json={}).json()["id"])
    assert final["status"] == "failed" and final["error"]["code"] == "NO_FILES"


def test_deleting_project_removes_files(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "Gone"}).json()["id"]
    upload(client, pid, "a.txt", b"hello")
    assert (tmp_path / "data" / "artifacts" / pid).is_dir()
    client.delete(f"/api/projects/{pid}?confirm=DELETE")
    assert not (tmp_path / "data" / "artifacts" / pid).exists()


def test_template_list_includes_ingestion():
    assert load_template("document-ingestion")["nodes"][0]["type"] == "agenticmind.data.document_loader"
