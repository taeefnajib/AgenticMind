# ADR-0034: Sandboxed code execution

**Status:** Accepted. Feature 2 of the open-source production plan (ADR-0032).

## Context
Engineers expect a "run this code" step in a workflow, and agents that write and run code to calculate,
transform data and check their reasoning. Until now, code outside the core nodes was refused in-process
(ADR-0004), and program of thought (ADR-0030) only evaluated arithmetic.

## Decision

### `agenticmind-sandbox` package
- **Contract:** `execute(language, code, inputs, limits) -> ExecutionResult`. Supported languages are
  `python` and `javascript`.
  - **Inside the sandbox:** a bootstrap program reads `{code, inputs}` from stdin and runs the code with
    `inputs`.
    - Python: set `result`, or define `main(inputs)`.
    - JavaScript: the code is an async function body that `return`s the value.
  - **Back to the caller:** what the code printed is captured, and one marker line returns the result or the
    exception with its traceback.
  - **Errors are results:** exceptions, timeouts and crashes come back as `ok=False`. A `SandboxError` is
    raised only when the sandbox cannot run at all (disabled, unavailable, network refused).
- **The operator chooses the backend, never the graph**, so an imported project cannot weaken isolation.
  `AGENTICMIND_SANDBOX` takes:
  - `docker` (default): a fresh container per execution with:
    - `--network none` (bridge only when the operator sets `AGENTICMIND_SANDBOX_ALLOW_NETWORK=1` and the node
      asks for it);
    - `--read-only` with a 64 MB `/tmp` tmpfs;
    - memory limits (with swap equal to memory), CPU limits and `--pids-limit 64`;
    - `--cap-drop ALL`, `no-new-privileges`, and user `65534`;
    - `docker kill` on timeout.
    - Images are pulled once, outside the execution timeout. The defaults are `python:3.12-slim` and
      `node:22-slim`, overridable with `AGENTICMIND_SANDBOX_PYTHON_IMAGE` / `AGENTICMIND_SANDBOX_NODE_IMAGE`
      (for example an image with pandas).
  - `process`: opt-in, for trusted code on your own machine. It provides:
    - Python isolated (`-I`), and Node under its permission model (`--permission`: no file reads or writes, no
      child processes or workers; verified);
    - a minimal environment (none of the server's keys), a temporary working directory and the timeout (the
      process group is killed);
    - on POSIX, limits on address space, CPU, file size and core dumps.
    - It does **not** block network access, and results carry a note saying so.
  - `off`: code nodes fail with `SANDBOX_DISABLED`.
- **Operator caps:** `AGENTICMIND_SANDBOX_MAX_TIMEOUT` (300 s) and `AGENTICMIND_SANDBOX_MAX_MEMORY_MB` (2048)
  override node settings.

### Nodes
- **Code** (`agenticmind.core.code@1.0.0`, Transform):
  - **Config:** language, code (a code editor in the IDE), timeout, memory and network.
  - **Data in:** the connected value is `inputs`.
  - **Outputs:** `result`, `stdout`, `error` and `report` (backend, duration, exit code, notes, stderr).
  - **Errors:** `on_error: fail` raises `CODE_FAILED` with the traceback; `output` returns it instead.
- **Code Interpreter Tool** (`agenticmind.agents.code_tool@1.0.0`):
  - The agent sends `code` and optional `inputs`.
  - It gets back `{result, stdout}`, or the exception with its traceback as a tool error, so it can fix the
    code and retry.
  - It needs `subprocess`, so it runs in Agent 1.2.0 / Tool Router 1.1.0 / Tool Chain 1.1.0 (ADR-0033);
    older versions refuse it.
- **IDE:** a `code` widget with a monospace editor where Tab indents.

### Template
**Analyse data with code (sandbox)**: a Code node summarising sales data, and an analyst Agent that computes
its answers with the Code Interpreter.

## Verification
- **Real executions in process mode** (on Windows locally and on Linux in CI):
  - results, `main()`, printed output;
  - exceptions with line numbers, `SystemExit`, non-JSON results;
  - timeouts, operator caps, and the environment not leaking;
  - JavaScript results and errors, with child processes and file reads refused by the permission model.
- **Docker:** the locked-down `docker run` flags are tested. A real Docker test (result, no network,
  read-only filesystem, timeout) runs wherever a daemon is available, such as the CI's Linux runners. It is
  skipped on this machine, where Docker was not running.
- **Nodes:** the Code node's errors, timeout and JavaScript; a clear message when Docker is missing; disabled
  and network refusals; an Agent fixing its code after seeing a `NameError` traceback; older Agents refusing
  the tool.
- **Found by the tests:** Windows text-mode stdout (`\r\n`) broke result parsing.
- **Live (OpenRouter / Gemini, process mode):** the template's Code node produced the region summary. The
  agent wrote and ran Python through `run_code` and answered that North's average price is 7.94% higher, which
  is correct.

## Consequences
- **Speed:** each execution is a fresh container (about a second of overhead) or process. Nothing persists
  between calls (no stateful notebook sessions yet).
- **Packages:** only the standard library unless the operator configures an image with packages. No pip
  installs at run time, because there is no network.
- **Process mode** is convenient but is not isolation: the docs and results say so, and Docker stays the
  default.

## Revisit when
Stateful interpreter sessions, file inputs and outputs (artifacts in and out), per-project images, or
microVM / gVisor runtimes are needed.
