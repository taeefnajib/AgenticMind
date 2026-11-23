"""Sandbox runner: real executions in process mode everywhere, and in Docker where a daemon is available."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from agenticmind_sandbox import Limits, Policy, SandboxError, docker_argv, execute, parse_output

PROCESS = Policy.from_env({"AGENTICMIND_SANDBOX": "process"})
HAS_NODE = shutil.which("node") is not None


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


async def test_python_result_stdout_and_main():
    r = await execute("python", "print('hi')\nresult = sum(inputs['xs'])", {"xs": [1, 2, 3]}, policy=PROCESS)
    assert r.ok and r.result == 6 and r.stdout == "hi" and r.backend == "process"
    r = await execute("python", "def main(inputs):\n    return {'n': len(inputs)}", "abcd", policy=PROCESS)
    assert r.result == {"n": 4}
    r = await execute("python", "result = {1, 2}", None, policy=PROCESS)
    assert sorted(r.result) == [1, 2]  # sets become lists


async def test_python_errors_are_results_not_exceptions():
    r = await execute("python", "x = 1\nraise ValueError('bad value')", None, policy=PROCESS)
    assert not r.ok and r.error["type"] == "ValueError" and r.error["message"] == "bad value"
    assert 'File "<code>", line 2' in r.error["traceback"]
    r = await execute("python", "import sys; sys.exit(3)", None, policy=PROCESS)
    assert not r.ok and r.error["type"] == "SystemExit"
    r = await execute("python", "result = float('nan')", None, policy=PROCESS)
    assert not r.ok and r.error["type"] == "ResultNotJSON"


async def test_timeout_kills_the_process():
    r = await execute("python", "while True: pass", None, Limits(timeout_seconds=1.5), policy=PROCESS)
    assert not r.ok and r.timed_out and r.error["type"] == "Timeout" and r.duration_ms < 15_000


async def test_process_mode_isolates_environment(monkeypatch):
    monkeypatch.setenv("AM_SECRET_FOR_TEST", "should-not-leak")
    r = await execute("python", "import os; result = os.environ.get('AM_SECRET_FOR_TEST')", None, policy=PROCESS)
    assert r.ok and r.result is None
    assert "not a security boundary" in r.notes[0]


@pytest.mark.skipif(not HAS_NODE, reason="node is not installed")
async def test_javascript_return_console_and_permissions():
    r = await execute("javascript", "console.log('hi', {a: 1});\nreturn inputs.xs.reduce((a, b) => a + b, 0);",
                      {"xs": [1, 2, 3]}, policy=PROCESS)  # fmt: skip
    assert r.ok and r.result == 6 and r.stdout == 'hi {"a":1}'
    r = await execute("javascript", "throw new TypeError('nope')", None, policy=PROCESS)
    assert not r.ok and r.error["type"] == "TypeError" and r.error["message"] == "nope"
    # Node's permission model: no child processes in process mode
    spawn = "return require('child_process').execSync('echo hi').toString()"
    r = await execute("javascript", spawn, None, policy=PROCESS)
    if any("no permission model" in n for n in r.notes):  # Node < 20: said so instead of pretending
        return
    assert not r.ok and "restricted" in r.error["message"]
    r = await execute("javascript", "return require('fs').readFileSync('README.md', 'utf8')", None, policy=PROCESS)
    assert not r.ok and "restricted" in r.error["message"]  # no file system reads either


async def test_policy_and_limits():
    with pytest.raises(SandboxError) as exc:
        await execute("python", "result = 1", policy=Policy.from_env({"AGENTICMIND_SANDBOX": "off"}))
    assert exc.value.code == "SANDBOX_DISABLED"
    with pytest.raises(SandboxError) as exc:
        await execute("python", "result = 1", limits=Limits(network=True), policy=PROCESS)
    assert exc.value.code == "SANDBOX_NETWORK_NOT_ALLOWED"
    with pytest.raises(SandboxError) as exc:
        Policy.from_env({"AGENTICMIND_SANDBOX": "chroot"})
    assert exc.value.code == "SANDBOX_MISCONFIGURED"
    assert Policy.from_env({}).backend == "docker"  # secure default
    capped = Policy.from_env({"AGENTICMIND_SANDBOX": "process", "AGENTICMIND_SANDBOX_MAX_TIMEOUT": "1"})
    r = await execute("python", "import time; time.sleep(5)", limits=Limits(timeout_seconds=60), policy=capped)
    assert r.timed_out  # the operator's cap wins over the node's timeout


def test_docker_argv_is_locked_down():
    argv = docker_argv("am-sbx-x", "python:3.12-slim", "python", Limits(memory_mb=128, cpus=0.5))
    joined = " ".join(argv)
    for flag in ("--network none", "--read-only", "--memory 128m", "--memory-swap 128m", "--cpus 0.5",
                 "--pids-limit 64", "--cap-drop ALL", "no-new-privileges", "--user 65534:65534", "--rm"):  # fmt: skip
        assert flag in joined, flag
    assert argv[-4:-1] == ["python", "-I", "-c"]
    assert "--network bridge" in " ".join(docker_argv("n", "i", "javascript", Limits(network=True)))


def test_parse_output_handles_crashes():
    crashed = parse_output(b"partial", b"Killed", 137)
    assert not crashed.ok and crashed.error["type"] == "SandboxCrash" and "memory" in crashed.error["message"]
    bad_flag = parse_output(b"", b"node: bad option: --nope\n", 9)
    assert bad_flag.error["message"] == "The code ended without a result: node: bad option: --nope"


def test_daemon_unreachable_is_not_a_missing_image():
    from agenticmind_sandbox.runner import _daemon_unreachable

    assert not _daemon_unreachable("Error response from daemon: No such image: python:3.12-slim")
    assert _daemon_unreachable("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
    assert _daemon_unreachable("failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine")


@pytest.mark.skipif(not docker_ready(), reason="Docker is not available")
async def test_docker_backend_runs_and_isolates():
    docker = Policy.from_env({"AGENTICMIND_SANDBOX": "docker"})
    r = await execute("python", "result = sum(inputs)", [1, 2, 3], Limits(timeout_seconds=60), policy=docker)
    assert r.ok and r.result == 6 and r.backend == "docker"
    net = await execute("python", "import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)", None,
                        Limits(timeout_seconds=60), policy=docker)  # fmt: skip
    assert not net.ok  # no network inside the container
    ro = await execute("python", "open('/etc/x', 'w')", None, Limits(timeout_seconds=60), policy=docker)
    assert not ro.ok  # read-only root file system, unprivileged user
    slow = await execute("python", "while True: pass", None, Limits(timeout_seconds=3), policy=docker)
    assert slow.timed_out
