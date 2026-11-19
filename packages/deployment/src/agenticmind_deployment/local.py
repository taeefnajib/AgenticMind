"""Local Docker deployer (ADR-0020): build the compiled app and run it with Docker Compose on this machine.

Blocking calls (``subprocess``); callers run them in a worker thread. Secret values are passed to
``docker compose`` through its process environment and referenced by name in the compose file, so they
are never written to disk. Every command's output is appended to the deployment log; it never contains
secret values because compose does not echo the environment.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

COMPOSE_FILE = "deploy/local/compose.yaml"


@dataclass
class LocalResult:
    ok: bool
    endpoint: str = ""
    error: str = ""


def docker_status(timeout: float = 15) -> tuple[bool, str]:
    exe = shutil.which("docker")
    if exe is None:
        return False, "Docker is not installed (or not on PATH)."
    try:
        out = subprocess.run(
            [exe, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Docker did not respond: {exc}"
    if out.returncode != 0 or not out.stdout.strip():
        return False, "The Docker daemon is not running. Start Docker Desktop (or the docker service)."
    return True, f"Docker {out.stdout.strip()}"


def free_port(start: int = 8100, end: int = 8199, taken: set[int] | None = None) -> int:
    for port in range(start, end + 1):
        if taken and port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free port between {start} and {end}")


def compose_project(project_id: str, deployment_id: str) -> str:
    return f"am-{project_id}-{deployment_id}".lower().replace("_", "-")


class LocalDocker:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def _run(self, args: list[str], *, cwd: Path, env: Mapping[str, str] | None = None, timeout: float) -> int:
        self.log("$ " + " ".join(args))
        full_env = {**os.environ, **(env or {})}
        with self.log_path.open("a", encoding="utf-8") as f:
            try:
                proc = subprocess.run(
                    args, cwd=cwd, env=full_env, stdout=f, stderr=subprocess.STDOUT, timeout=timeout, text=True
                )
            except subprocess.TimeoutExpired:
                f.write(f"(timed out after {timeout:.0f}s)\n")
                return 124
        return proc.returncode

    def up(
        self, workdir: Path, project: str, *, secrets: Mapping[str, str], port: int, build_timeout: float = 1800
    ) -> LocalResult:
        code = self._run(
            ["docker", "compose", "-p", project, "-f", COMPOSE_FILE, "up", "-d", "--build", "--wait",
             "--wait-timeout", "300"],
            cwd=workdir,
            env={**secrets, "AM_HOST_PORT": str(port)},
            timeout=build_timeout,
        )  # fmt: skip
        if code != 0:
            return LocalResult(False, error=f"docker compose up failed (exit {code}); see the deployment log")
        endpoint = f"http://127.0.0.1:{port}"
        if not health(endpoint, attempts=20):
            return LocalResult(False, endpoint, "The container started but /healthz did not answer")
        self.log(f"healthy: {endpoint}/healthz")
        return LocalResult(True, endpoint)

    def down(self, workdir: Path, project: str, *, timeout: float = 180) -> bool:
        # keep volumes: the project's data volume is shared with other deployments (rollback keeps the index)
        # compose interpolates the whole file even for `down`, so give the port variable a value
        return self._run(["docker", "compose", "-p", project, "-f", COMPOSE_FILE, "down"], cwd=workdir,
                         env={"AM_HOST_PORT": "0"}, timeout=timeout) == 0  # fmt: skip


def health(endpoint: str, *, attempts: int = 1, delay: float = 1.5, timeout: float = 5) -> bool:
    for i in range(attempts):
        try:
            with urllib.request.urlopen(f"{endpoint.rstrip('/')}/healthz", timeout=timeout) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except OSError:
            pass
        if i + 1 < attempts:
            time.sleep(delay)
    return False
