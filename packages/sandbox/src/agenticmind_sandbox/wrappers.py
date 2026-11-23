"""Bootstrap programs that run *inside* the sandbox.

Each reads ``{"code": ..., "inputs": ...}`` as JSON on stdin, runs the user's code with ``inputs`` available,
captures what it prints, and writes one marker line ``__AM_RESULT__{json}`` to the real stdout:
``{"ok": true, "result": ..., "stdout": "..."}`` or ``{"ok": false, "error": {type, message, traceback}, ...}``.

* Python: set ``result = ...``, or define ``main(inputs)`` and return the value.
* JavaScript: the code is the body of an async function receiving ``inputs``; ``return`` the value.
"""

from __future__ import annotations

MARKER = "__AM_RESULT__"
MAX_CAPTURED_STDOUT = 200_000

PYTHON = r"""
import contextlib, io, json, sys, traceback
LIMIT = __LIMIT__
def _default(o):
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if hasattr(o, "tolist"):
        return o.tolist()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return repr(o)
payload = json.loads(sys.stdin.read())
buf = io.StringIO()
ns = {"inputs": payload.get("inputs"), "__name__": "__sandbox__"}
try:
    with contextlib.redirect_stdout(buf):
        exec(compile(payload["code"], "<code>", "exec"), ns)
        if "result" not in ns and callable(ns.get("main")):
            ns["result"] = ns["main"](payload.get("inputs"))
    res = {"ok": True, "result": ns.get("result")}
except BaseException as e:
    tb = traceback.format_exc(limit=20)
    res = {"ok": False, "error": {"type": type(e).__name__, "message": str(e), "traceback": tb}}
res["stdout"] = buf.getvalue()[-LIMIT:]
try:
    line = json.dumps(res, default=_default, allow_nan=False)
except (TypeError, ValueError) as e:
    line = json.dumps({"ok": False, "stdout": res["stdout"], "error": {"type": "ResultNotJSON",
                       "message": "The result is not JSON-serialisable: %s" % e, "traceback": ""}})
sys.__stdout__.write("\n__MARKER__" + line + "\n")
sys.__stdout__.flush()
""".replace("__LIMIT__", str(MAX_CAPTURED_STDOUT)).replace("__MARKER__", MARKER)

JAVASCRIPT = r"""
const LIMIT = __LIMIT__;
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', async () => {
  const p = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const logs = [];
  const fmt = (a) => a.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' ');
  const con = { log: (...a) => logs.push(fmt(a)), info: (...a) => logs.push(fmt(a)),
                warn: (...a) => logs.push(fmt(a)), error: (...a) => logs.push(fmt(a)) };
  let res;
  try {
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const fn = new AsyncFunction('inputs', 'console', p.code);
    const value = await fn(p.inputs, con);
    res = { ok: true, result: value === undefined ? null : value };
  } catch (e) {
    res = { ok: false, error: { type: (e && e.name) || 'Error', message: String((e && e.message) || e),
                                traceback: String((e && e.stack) || '') } };
  }
  res.stdout = logs.join('\n').slice(-LIMIT);
  let line;
  try { line = JSON.stringify(res); } catch (e) {
    line = JSON.stringify({ ok: false, stdout: res.stdout,
      error: { type: 'ResultNotJSON', message: 'The result is not JSON-serialisable: ' + e.message, traceback: '' } });
  }
  process.stdout.write('\n__MARKER__' + line + '\n');
});
""".replace("__LIMIT__", str(MAX_CAPTURED_STDOUT)).replace("__MARKER__", MARKER)
