"""Run official checks against an owned, isolated server (no Key needed).

From repository root: starter/.venv/Scripts/python.exe scripts/verify.py eval|preflight
Only this script's child process is stopped. Existing services are never touched.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["eval", "preflight"])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = (args.out or ROOT / "starter" / "var" / ("audit-" + args.mode)).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    process = None
    with tempfile.TemporaryDirectory(prefix="moneki-verify-", ignore_cleanup_errors=True) as temp, (out / "server.log").open("w", encoding="utf-8") as log:
        def start(extra=None):
            nonlocal process
            env = os.environ.copy()
            for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT", "CHAT_BUDGET", "DATA_DIR", "KB_DIR", "TODAY"):
                env.pop(key, None)
            env.update(VAR_DIR=str(Path(temp) / "var"), INDEX_PATH=str(Path(temp) / "index.json"), PYTHONUTF8="1")
            env.update(extra or {})
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "kbqa.server:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT / "starter", env=env, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Backend exited; see {out / 'server.log'}")
                try:
                    with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
                        if response.status == 200:
                            return
                except (OSError, TimeoutError):
                    time.sleep(0.2)
            raise RuntimeError("Backend did not become ready in 30 seconds")
        try:
            if args.mode == "preflight":
                sys.path.insert(0, str(ROOT / "eval"))
                from llm_gateway import run_preflight
                result = run_preflight(service_url=url, no_wait=True, out_dir=str(out), ready_hook=start)
                return 0 if result.passed else 1
            start()
            code = subprocess.call([sys.executable, str(ROOT / "eval" / "run_eval.py"), "--base-url", url, "--questions", str(ROOT / "eval" / "public_questions.jsonl"), "--out", str(out)], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"))
            if code:
                return code
            total = json.loads((out / "report.json").read_text(encoding="utf-8"))["total"]
            # 官方脚本即使掉分也返回 0；CI 必须另查报告，不能假绿。
            return 0 if total["earned"] == total["points"] else 1
        finally:
            if process is not None:
                if os.name == "nt" and process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
