"""Run untrusted Python / JavaScript with limits (ADR-0034).

The backend is chosen by the operator, never by a graph (a shared project must not be able to weaken
isolation): ``AGENTICMIND_SANDBOX`` = ``docker`` (default), ``process`` or ``off``.

* **docker**: a fresh container per execution with no network (unless the operator allows it and the node
  asks), a read-only root filesystem, a small ``/tmp`` tmpfs, memory / CPU / process limits, all Linux
  capabilities dropped, ``no-new-privileges``, an unprivileged user, and a hard kill on timeout.
* **process**: a separate interpreter on this machine for trusted local use. Python runs isolated (``-I``) and
  JavaScript under Node's permission model (no file system writes, child processes or workers); both get a
  minimal environment, a temporary working directory and the timeout, plus memory / CPU / file-size limits on
  Linux and macOS. It does **not** block network access and is not a security boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .wrappers import JAVASCRIPT, MARKER, PYTHON

Language = Literal["python", "javascript"]
Backend = Literal["docker", "process", "off"]
MAX_PAYLOAD_BYTES = 5_000_000
MAX_OUTPUT_BYTES = 5_000_000


class SandboxError(Exception):
    """The sandbox could not run the code at all (disabled, unavailable, misconfigured)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Limits:
    timeout_seconds: float = 30.0
    memory_mb: int = 256
    cpus: float = 1.0
    network: bool = False


@dataclass
class ExecutionResult:
    ok: bool
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: dict[str, Any] | None = None
    timed_out: bool = False
    exit_code: int | None = None
    duration_ms: float = 0.0
    backend: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Policy:
    """Operator settings, read from the environment at call time."""

    backend: Backend
    allow_network: bool
    max_timeout_seconds: float
    max_memory_mb: int
    python_image: str
    node_image: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Policy:
        e = os.environ if env is None else env
        backend = e.get("AGENTICMIND_SANDBOX", "docker").strip().lower() or "docker"
        if backend not in ("docker", "process", "off"):
            raise SandboxError(
                "SANDBOX_MISCONFIGURED", f"AGENTICMIND_SANDBOX must be docker, process or off, not {backend!r}"
            )
        return cls(
            backend=backend,  # type: ignore[arg-type]
            allow_network=e.get("AGENTICMIND_SANDBOX_ALLOW_NETWORK", "0").lower() in ("1", "true", "yes", "on"),
            max_timeout_seconds=float(e.get("AGENTICMIND_SANDBOX_MAX_TIMEOUT", "300")),
            max_memory_mb=int(e.get("AGENTICMIND_SANDBOX_MAX_MEMORY_MB", "2048")),
            python_image=e.get("AGENTICMIND_SANDBOX_PYTHON_IMAGE", "python:3.12-slim"),
            node_image=e.get("AGENTICMIND_SANDBOX_NODE_IMAGE", "node:22-slim"),
        )


def _payload(code: str, inputs: Any) -> bytes:
    data = json.dumps({"code": code, "inputs": inputs}, ensure_ascii=False, default=str).encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise SandboxError(
            "SANDBOX_INPUT_TOO_LARGE", f"code + inputs are {len(data):,} bytes (max {MAX_PAYLOAD_BYTES:,})"
        )
    return data


def parse_output(stdout: bytes, stderr: bytes, exit_code: int | None) -> ExecutionResult:
    text = stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")  # Windows text-mode stdout
    err = stderr.decode("utf-8", errors="replace")[-20_000:]
    at = text.rfind("\n" + MARKER)
    if at < 0:
        message = "The code ended without a result"
        if exit_code == 137 or (exit_code is not None and exit_code < 0):
            message = "The sandbox killed the process (memory limit or a crash)"
        first = next((ln.strip() for ln in err.splitlines() if ln.strip()), "")
        if first:
            message += f": {first[:300]}"
        tail = (err or text).strip()[-2000:]
        return ExecutionResult(ok=False, stdout=text[-20_000:], stderr=err, exit_code=exit_code,
                               error={"type": "SandboxCrash", "message": message, "traceback": tail})  # fmt: skip
    leading = text[:at]
    line = text[at + 1 + len(MARKER) :].strip().splitlines()[0]
    data = json.loads(line)
    stdout_text = (leading + ("\n" if leading and data.get("stdout") else "") + data.get("stdout", "")).strip("\n")
    return ExecutionResult(
        ok=bool(data.get("ok")),
        result=data.get("result"),
        stdout=stdout_text,
        stderr=err,
        error=data.get("error"),
        exit_code=exit_code,
    )


async def _communicate(
    proc: asyncio.subprocess.Process, payload: bytes, timeout: float, on_timeout: Any
) -> tuple[bytes, bytes, bool]:
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout)
        return out[-MAX_OUTPUT_BYTES:], err[-MAX_OUTPUT_BYTES:], False
    except TimeoutError:
        await on_timeout()
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 10)
        except (TimeoutError, ValueError):
            out, err = b"", b""
        return out[-MAX_OUTPUT_BYTES:], err[-MAX_OUTPUT_BYTES:], True


# ---- docker -----------------------------------------------------------------------------------------
def docker_argv(name: str, image: str, language: Language, limits: Limits) -> list[str]:
    argv = [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", "bridge" if limits.network else "none",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
        "--cpus", f"{limits.cpus:g}", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534", "--workdir", "/tmp",
        "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--label", "agenticmind.sandbox=1",
        image,
    ]  # fmt: skip
    if language == "python":
        return [*argv, "python", "-I", "-c", PYTHON]
    return [*argv, "node", "-e", JAVASCRIPT]


async def _run_quiet(*argv: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout:g}s"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


async def ensure_image(image: str) -> None:
    """Pull the image once (outside the execution timeout) so the first run does not time out pulling."""
    if shutil.which("docker") is None:
        raise SandboxError("SANDBOX_UNAVAILABLE", _DOCKER_HELP.format(detail="the docker command is not installed"))
    code, out = await _run_quiet("docker", "image", "inspect", "--format", "{{.Id}}", image, timeout=30)
    if code == 0:
        return
    if _daemon_unreachable(out):
        raise SandboxError("SANDBOX_UNAVAILABLE", _DOCKER_HELP.format(detail=out.strip().splitlines()[-1][:300]))
    code, out = await _run_quiet("docker", "pull", "--quiet", image, timeout=600)
    if code != 0:
        raise SandboxError("SANDBOX_IMAGE_UNAVAILABLE", f"Could not pull {image}: {out.strip()[-300:]}")


_UNREACHABLE = (
    "cannot connect",
    "failed to connect",
    "is the docker daemon running",
    "connection refused",
    "error during connect",
    "permission denied while trying to connect",
)


def _daemon_unreachable(output: str) -> bool:
    """True when Docker itself cannot be reached (not e.g. "No such image", which also mentions the daemon)."""
    low = output.lower()
    return any(marker in low for marker in _UNREACHABLE)


_DOCKER_HELP = (
    "Code runs in Docker containers, but Docker is not available ({detail}). Start Docker, or (only for trusted "
    "code on your own machine) set AGENTICMIND_SANDBOX=process."
)


async def _run_docker(language: Language, code: str, inputs: Any, limits: Limits, policy: Policy) -> ExecutionResult:
    image = policy.python_image if language == "python" else policy.node_image
    await ensure_image(image)
    name = f"am-sbx-{uuid.uuid4().hex[:12]}"
    proc = await asyncio.create_subprocess_exec(
        *docker_argv(name, image, language, limits),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip

    async def kill() -> None:
        await _run_quiet("docker", "kill", name, timeout=20)
        if proc.returncode is None:
            proc.kill()

    out, err, timed_out = await _communicate(proc, _payload(code, inputs), limits.timeout_seconds, kill)
    if proc.returncode == 125 and MARKER.encode() not in out:  # docker itself failed (bad flag, daemon gone)
        raise SandboxError(
            "SANDBOX_UNAVAILABLE", _DOCKER_HELP.format(detail=err.decode(errors="replace").strip()[-300:])
        )
    result = parse_output(out, err, proc.returncode)
    result.timed_out = timed_out
    return result


# ---- process ----------------------------------------------------------------------------------------
def _process_env(tmp: str) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "PATHEXT", "COMSPEC", "LANG", "LC_ALL")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update({"HOME": tmp, "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp, "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8"})  # fmt: skip
    return env


def _posix_limits(limits: Limits) -> Any:
    if sys.platform == "win32":
        return None

    def apply() -> None:  # runs in the child before exec
        import resource

        mem = limits.memory_mb * 1024 * 1024
        for res, value in ((resource.RLIMIT_AS, mem), (resource.RLIMIT_CPU, int(limits.timeout_seconds) + 1),
                           (resource.RLIMIT_FSIZE, 50 * 1024 * 1024), (resource.RLIMIT_CORE, 0)):  # fmt: skip
            try:
                resource.setrlimit(res, (value, value))
            except (ValueError, OSError):
                pass

    return apply


_NODE_FLAGS: dict[str, str | None] = {}


async def _node_permission_flag(node: str) -> str | None:
    """``--permission`` (Node 22.13+ / 23.5+), ``--experimental-permission`` (Node 20 to 22.12), else None."""
    if node in _NODE_FLAGS:
        return _NODE_FLAGS[node]
    code, out = await _run_quiet(node, "--version", timeout=15)
    flag: str | None = None
    try:
        major, minor = (int(x) for x in out.strip().lstrip("v").split(".")[:2])
    except ValueError:
        major = minor = 0
    if code == 0:
        if (major, minor) >= (23, 5) or (major == 22 and minor >= 13):
            flag = "--permission"
        elif major >= 20:
            flag = "--experimental-permission"
    _NODE_FLAGS[node] = flag
    return flag


async def _run_process(language: Language, code: str, inputs: Any, limits: Limits) -> ExecutionResult:
    notes = ["process mode: not a security boundary; network access is not blocked"]
    if language == "python":
        argv = [sys.executable, "-I", "-c", PYTHON]
    else:
        node = shutil.which("node")
        if node is None:
            raise SandboxError("SANDBOX_UNAVAILABLE", "JavaScript needs Node.js (node) on this machine")
        flag = await _node_permission_flag(node)
        if flag is None:
            notes.append("this Node.js has no permission model (needs Node 20+): file system access is not restricted")
        argv = [node, *([flag] if flag else []), f"--max-old-space-size={max(limits.memory_mb, 32)}", "-e", JAVASCRIPT]
    with tempfile.TemporaryDirectory(prefix="am-sbx-") as tmp:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=tmp, env=_process_env(tmp),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=_posix_limits(limits), start_new_session=sys.platform != "win32",
        )  # fmt: skip

        async def kill() -> None:
            if sys.platform != "win32":
                import signal

                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.returncode is None:
                proc.kill()

        out, err, timed_out = await _communicate(proc, _payload(code, inputs), limits.timeout_seconds, kill)
    result = parse_output(out, err, proc.returncode)
    result.timed_out = timed_out
    result.notes = notes
    return result


# ---- entry point ------------------------------------------------------------------------------------
async def execute(language: Language, code: str, inputs: Any = None, limits: Limits | None = None,
                  *, policy: Policy | None = None) -> ExecutionResult:  # fmt: skip
    """Run ``code`` with ``inputs``. Raises ``SandboxError`` when it cannot run at all; errors in the code
    itself (exceptions, timeouts, memory) come back as ``ExecutionResult(ok=False, ...)``."""
    policy = policy or Policy.from_env()
    limits = limits or Limits()
    if policy.backend == "off":
        raise SandboxError("SANDBOX_DISABLED", "Code execution is turned off on this server (AGENTICMIND_SANDBOX=off)")
    if language not in ("python", "javascript"):
        raise SandboxError("SANDBOX_LANGUAGE", f"Unsupported language {language!r}")
    if limits.network and not policy.allow_network:
        raise SandboxError(
            "SANDBOX_NETWORK_NOT_ALLOWED",
            "This code asks for network access, which the server does not allow (AGENTICMIND_SANDBOX_ALLOW_NETWORK=1)",
        )
    limits = Limits(
        timeout_seconds=min(limits.timeout_seconds, policy.max_timeout_seconds),
        memory_mb=min(limits.memory_mb, policy.max_memory_mb),
        cpus=limits.cpus,
        network=limits.network,
    )
    started = time.perf_counter()
    if policy.backend == "docker":
        result = await _run_docker(language, code, inputs, limits, policy)
    else:
        result = await _run_process(language, code, inputs, limits)
    result.backend = policy.backend
    result.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if result.timed_out:
        result.ok = False
        result.error = {"type": "Timeout", "message": f"The code ran longer than {limits.timeout_seconds:g}s",
                        "traceback": ""}  # fmt: skip
    return result
