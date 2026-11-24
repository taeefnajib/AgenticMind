"""Durable execution (ADR-0039): the job queue, leases, recovery from dead workers, cross-process cancel."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_runtime.secrets import MappingSecretResolver
from fastapi.testclient import TestClient


def graph(delay: float) -> dict:
    return {
        "ir_version": "1.0",
        "project": {"id": "template", "name": "Durable"},
        "nodes": [
            {
                "id": "q",
                "type": "agenticmind.core.manual_input",
                "version": "1.0.0",
                "config": {"key": "q", "value_type": "String", "default": "world"},
            },
            {
                "id": "greet",
                "type": "agenticmind.core.text_template",
                "version": "1.0.0",
                "config": {"template": "Hello {{ text }}!", "strict": True},
            },
            {"id": "wait", "type": "agenticmind.core.delay", "version": "1.0.0", "config": {"seconds": delay}},
            {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "greeting"}},
        ],
        "edges": [
            {
                "id": "e1",
                "kind": "data",
                "source": {"node": "q", "port": "value"},
                "target": {"node": "greet", "port": "text"},
            },
            {
                "id": "e2",
                "kind": "data",
                "source": {"node": "greet", "port": "text"},
                "target": {"node": "wait", "port": "value"},
            },
            {
                "id": "e3",
                "kind": "data",
                "source": {"node": "wait", "port": "value"},
                "target": {"node": "out", "port": "value"},
            },
        ],
        "metadata": {},
    }


def settings_for(tmp_path: Path, **kw) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'shared.db').as_posix()}",
        data_dir=tmp_path / "data",
        auth_mode="off",
        mcp_server=False,
        **kw,
    )


def wait_status(get, statuses, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = get()
        if row and row["status"] in statuses:
            return row
        time.sleep(0.1)
    raise AssertionError(f"status not in {statuses}: {get()}")


# ---- queue primitives ---------------------------------------------------------------------------
def test_claims_are_exclusive_and_leases_expire(tmp_path):
    app = create_app(settings_for(tmp_path, embedded_worker=False), secrets=MappingSecretResolver({}))
    svc = app.state.services
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "D", "document": graph(0)}).json()["id"]
        run_id = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"  # no worker in this process
        store = svc.store
        [job] = store.claim_jobs("worker-a", lease_seconds=0.5, limit=5)
        assert job.run_id == run_id and job.attempts == 1
        assert store.claim_jobs("worker-b", 30, 5) == []  # held by worker-a
        assert store.renew_lease(run_id, "worker-a", 0.5) == (True, False)
        time.sleep(0.7)  # worker-a stops renewing (it died)
        [again] = store.claim_jobs("worker-b", 30, 5)
        assert again.owner == "worker-b" and again.attempts == 2
        assert store.renew_lease(run_id, "worker-a", 30) == (False, False)  # worker-a lost it
        assert store.request_cancel(run_id) == "requested" and store.renew_lease(run_id, "worker-b", 30) == (True, True)
        # leader leases
        assert store.acquire_lease("test-lease", "p1", 0.5) and not store.acquire_lease("test-lease", "p2", 0.5)
        assert store.acquire_lease("test-lease", "p1", 0.5)  # renew
        time.sleep(0.7)
        assert store.acquire_lease("test-lease", "p2", 30) and not store.acquire_lease("test-lease", "p1", 30)


def test_queued_cancel_and_graceful_handover(tmp_path):
    shared = settings_for(tmp_path, embedded_worker=False)
    app = create_app(shared, secrets=MappingSecretResolver({}))
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "D", "document": graph(0)}).json()["id"]
        queued = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
        assert client.post(f"/api/runs/{queued}/cancel").status_code == 200
        assert client.get(f"/api/runs/{queued}").json()["status"] == "cancelled"  # never started
    # a run interrupted by a graceful shutdown is handed back and finished by the next process
    first = create_app(settings_for(tmp_path), secrets=MappingSecretResolver({}))
    first.state.services.runs.max_concurrency = 4
    with TestClient(first) as client:
        pid2 = client.post("/api/projects", json={"name": "Slow", "document": graph(30)}).json()["id"]
        run_id = client.post(f"/api/projects/{pid2}/runs", json={"inputs": {}}).json()["id"]
        wait_status(lambda: client.get(f"/api/runs/{run_id}").json(), {"running"})
        time.sleep(0.5)
    job = first.state.services.store.get_job(run_id)
    assert job.status == "queued" and job.owner is None  # released, not failed
    events_before = len(first.state.services.store.list_events(run_id))
    nxt = create_app(settings_for(tmp_path), secrets=MappingSecretResolver({}))
    store = nxt.state.services.store
    doc = graph(0)
    doc["project"]["id"] = pid2
    rev = store.save_revision(pid2, __import__("agenticmind_ir").load_project(doc), None)[1]  # make the rest fast
    with store._sf.begin() as s:  # the queued run keeps its snapshot; shorten its delay for the test
        from agenticmind_api.db import RunRow

        s.get(RunRow, run_id).ir_snapshot = rev.document
    with TestClient(nxt) as client:
        done = wait_status(lambda: client.get(f"/api/runs/{run_id}").json(), {"succeeded", "failed"})
        assert done["status"] == "succeeded" and done["outputs"] == {"greeting": "Hello world!"}
        events = store.list_events(run_id)
        assert len(events) > events_before
        greet_starts = [e for e in events if e.event_type == "node_started" and e.node_id == "greet"]
        assert len(greet_starts) == 1  # restored from its checkpoint, not run again


# ---- a real worker process killed mid-run ---------------------------------------------------------
def spawn_worker(tmp_path: Path, name: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENTICMIND_DATABASE_URL": f"sqlite:///{(tmp_path / 'shared.db').as_posix()}",
        "AGENTICMIND_DATA_DIR": str(tmp_path / "data"),
        "AGENTICMIND_WORKER_ID": name,
        "AGENTICMIND_LEASE_SECONDS": "2",
        "AGENTICMIND_AUTH": "off",
        "AGENTICMIND_MCP_SERVER": "0",
    }
    log = open(tmp_path / f"{name}.log", "w")  # noqa: SIM115 - closed with the process
    return subprocess.Popen([sys.executable, "-m", "agenticmind_api.worker"], env=env, stdout=log, stderr=log)


def test_killed_worker_is_recovered_and_cancel_crosses_processes(tmp_path):
    api = create_app(settings_for(tmp_path, embedded_worker=False), secrets=MappingSecretResolver({}))
    store = api.state.services.store
    with TestClient(api) as client:
        pid = client.post("/api/projects", json={"name": "Crash", "document": graph(4)}).json()["id"]
        run_id = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
        get = lambda: client.get(f"/api/runs/{run_id}").json()  # noqa: E731
        worker_a = spawn_worker(tmp_path, "worker-a")
        try:
            wait_status(get, {"running"}, timeout=60)
            deadline = time.time() + 30  # until the slow node is in flight (greet already checkpointed)
            while time.time() < deadline and not any(
                e.event_type == "node_started" and e.node_id == "wait" for e in store.list_events(run_id)
            ):
                time.sleep(0.1)
            worker_a.kill()  # hard crash: no shutdown, no lease release
            worker_a.wait(10)
        finally:
            if worker_a.poll() is None:
                worker_a.kill()
        assert get()["status"] == "running" and store.get_job(run_id).owner == "worker-a"
        worker_b = spawn_worker(tmp_path, "worker-b")
        try:
            done = wait_status(get, {"succeeded", "failed"}, timeout=60)
            assert done["status"] == "succeeded", (tmp_path / "worker-b.log").read_text()
            assert done["outputs"] == {"greeting": "Hello world!"}
            deadline = time.time() + 10  # the job is closed right after the run is recorded as finished
            while store.get_job(run_id).status != "done" and time.time() < deadline:
                time.sleep(0.1)
            job = store.get_job(run_id)
            assert job.owner == "worker-b" and job.attempts == 2 and job.status == "done"
            events = store.list_events(run_id)
            assert len([e for e in events if e.event_type == "node_started" and e.node_id == "greet"]) == 1
            assert len([e for e in events if e.event_type == "node_started" and e.node_id == "wait"]) == 2

            # cancel from this process while worker-b executes another run
            slow = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
            wait_status(lambda: client.get(f"/api/runs/{slow}").json(), {"running"}, timeout=60)
            assert client.post(f"/api/runs/{slow}/cancel").status_code == 200
            cancelled = wait_status(
                lambda: client.get(f"/api/runs/{slow}").json(), {"cancelled", "failed", "succeeded"}
            )
            assert cancelled["status"] == "cancelled"
            # a client in another process can wait for runs it did not execute
            third = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
            client.portal.call(api.state.services.runs.wait, third, 60)
            assert client.get(f"/api/runs/{third}").json()["status"] == "succeeded"
        finally:
            worker_b.terminate()
            try:
                worker_b.wait(15)
            except subprocess.TimeoutExpired:
                worker_b.kill()


def test_one_trigger_leader_across_processes(tmp_path):
    a = create_app(settings_for(tmp_path, worker_id="api-a", lease_seconds=2), secrets=MappingSecretResolver({}))
    b = create_app(settings_for(tmp_path, worker_id="api-b", lease_seconds=2), secrets=MappingSecretResolver({}))
    with TestClient(a), TestClient(b) as client_b:
        ta, tb = a.state.services.triggers, b.state.services.triggers
        assert [ta.leader, tb.leader].count(True) == 1  # exactly one schedules and polls
        leader_was_a = ta.leader
    # after the leader stops (lease released), the other process takes over
    with TestClient(b) if leader_was_a else TestClient(a) as client:
        survivor = (b if leader_was_a else a).state.services.triggers
        assert survivor.leader is True
        del client
    del client_b
