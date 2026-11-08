"""agent-sandbox: runs short Python snippets for the compute agent.

Security model (see README):
* Holds no secrets: no env secrets, no service-account token, nothing worth stealing.
* No network: a NetworkPolicy denies all egress and allows ingress only from agent-compute.
* Every run gets a fresh temp dir and an empty environment; `python -I -B` ignores user site and env vars.
* Limits per run: wall time, CPU seconds, address space, output file size, open files; the whole process group
  is killed on timeout. Output goes to size-capped files, never to memory.
* Container: non-root, read-only root filesystem, all capabilities dropped, /work is a small memory tmpfs.

Standard library only (no third-party code in the image). API:
    POST /v1/run   {"language": "python", "code": "...", "timeoutS": 10}
                   -> {"exitCode": 0, "stdout": "...", "stderr": "...", "timedOut": false, "durationMs": 12}
    GET  /healthz  -> {"status": "ok"}
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
WORK_ROOT = os.environ.get("WORK_ROOT", "/work")
MAX_CODE_BYTES = int(os.environ.get("MAX_CODE_BYTES", "20000"))
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", "65536"))
DEFAULT_TIMEOUT_S = float(os.environ.get("DEFAULT_TIMEOUT_S", "10"))
MAX_TIMEOUT_S = float(os.environ.get("MAX_TIMEOUT_S", "30"))
MEMORY_LIMIT_MB = int(os.environ.get("MEMORY_LIMIT_MB", "256"))
MAX_CONCURRENT_RUNS = int(os.environ.get("MAX_CONCURRENT_RUNS", "4"))
HEALTH_PATHS = {"/healthz", "/readyz"}

_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)

# Applied inside the child before it becomes the user's program (no preexec_fn: not safe with threads).
_LAUNCHER = """
import os, resource, sys
cpu, mem, fsize = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
for limit, value in ((resource.RLIMIT_CPU, cpu), (resource.RLIMIT_AS, mem), (resource.RLIMIT_FSIZE, fsize),
                     (resource.RLIMIT_NOFILE, 32), (resource.RLIMIT_CORE, 0)):
    resource.setrlimit(limit, (value, value))
os.execv(sys.executable, [sys.executable, "-I", "-B", "main.py"])
"""


def _log(event: str, **fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "level": "INFO",
              "logger": "agent_sandbox", "event": event, "service": "agent-sandbox", **fields}
    print(json.dumps(record), flush=True)


def _read_capped(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read(MAX_OUTPUT_BYTES + 1)
    text = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return text + ("\n...(output truncated)" if len(data) > MAX_OUTPUT_BYTES else "")


def run_python(code: str, timeout_s: float) -> dict:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=WORK_ROOT) as cwd:
        with open(os.path.join(cwd, "main.py"), "w", encoding="utf-8") as f:
            f.write(code)
        out_path, err_path = os.path.join(cwd, "stdout.txt"), os.path.join(cwd, "stderr.txt")
        timed_out = False
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _LAUNCHER, str(int(timeout_s) + 1), str(MEMORY_LIMIT_MB * 1024 * 1024),
                 str(MAX_OUTPUT_BYTES * 4)],
                cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True, start_new_session=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": cwd, "TMPDIR": cwd, "PYTHONIOENCODING": "utf-8",
                     "LANG": "C.UTF-8", "TZ": "UTC"},
            )
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)   # also stops anything the snippet started
                except ProcessLookupError:
                    pass
                proc.wait()
        return {"exitCode": proc.returncode if not timed_out else 124, "stdout": _read_capped(out_path),
                "stderr": _read_capped(err_path) + ("\nTimed out." if timed_out else ""), "timedOut": timed_out,
                "durationMs": round((time.perf_counter() - started) * 1000)}


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-sandbox"
    sys_version = ""

    def log_message(self, *args) -> None:   # no default access log; runs are logged as JSON below
        pass

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in HEALTH_PATHS:
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/run":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_CODE_BYTES + 1024:
            return self._send(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(length))
            code, language = str(body["code"]), str(body.get("language", "python")).lower()
            timeout_s = min(max(float(body.get("timeoutS") or DEFAULT_TIMEOUT_S), 1.0), MAX_TIMEOUT_S)
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"error": "invalid request"})
        if language != "python":
            return self._send(400, {"error": "only python is supported"})
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return self._send(413, {"error": "code too large"})
        if not _slots.acquire(blocking=False):
            return self._send(429, {"error": "sandbox busy"})
        try:
            result = run_python(code, timeout_s)
        finally:
            _slots.release()
        _log("sandbox.run", exit_code=result["exitCode"], timed_out=result["timedOut"],
             duration_ms=result["durationMs"], code_bytes=len(code))
        self._send(200, result)


def main() -> None:
    os.makedirs(WORK_ROOT, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    _log("service.started", port=PORT, max_concurrent_runs=MAX_CONCURRENT_RUNS)
    server.serve_forever()


if __name__ == "__main__":
    main()
