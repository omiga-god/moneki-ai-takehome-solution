"""Windows double-click launcher; stdlib bootstrap, owned server, no global changes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'work' / 'launcher'
PYTHON = ROOT / 'starter' / '.venv' / 'Scripts' / 'python.exe'


def run(command, cwd=ROOT):
    subprocess.run([str(item) for item in command], cwd=cwd, check=True)


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def prepare():
    if not PYTHON.is_file():
        print('[1/4] Creating Python environment...', flush=True)
        run([sys.executable, '-m', 'venv', PYTHON.parents[1]])
    requirements = ROOT / 'starter' / 'requirements.txt'
    check = "import importlib.metadata as m,sys; from pathlib import Path; pairs=[x.strip().split('==') for x in Path(sys.argv[1]).read_text().splitlines() if x.strip() and not x.startswith('#')]; sys.exit(0 if all(m.version(n)==v for n,v in pairs) else 1)"
    result = subprocess.run([str(PYTHON), '-c', check, str(requirements)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        print('[1/4] Installing Python dependencies...', flush=True)
        run([PYTHON, '-m', 'pip', 'install', '-r', requirements])
    frontend = ROOT / 'frontend'
    sources = []
    for directory, folders, files in os.walk(frontend):
        folders[:] = [name for name in folders if name not in {'node_modules', 'dist'}]
        sources.extend(Path(directory) / name for name in files)
    source_hash = fingerprint(sources)
    stamp = WORK / 'frontend.sha256'
    if not (frontend / 'dist' / 'index.html').is_file() or not stamp.exists() or stamp.read_text() != source_hash:
        npm = shutil.which('npm.cmd') or shutil.which('npm')
        if not npm:
            raise RuntimeError('Node.js 22 LTS with npm is required for the first build. Install it and restart this launcher.')
        lock_hash = fingerprint([frontend / 'package.json', frontend / 'package-lock.json'])
        deps_stamp = WORK / 'npm.sha256'
        if not (frontend / 'node_modules').is_dir() or not deps_stamp.exists() or deps_stamp.read_text() != lock_hash:
            print('[2/4] Installing frontend dependencies (first launch may take several minutes)...', flush=True)
            run([npm, 'ci', '--cache', WORK / 'npm-cache', '--registry=https://registry.npmjs.org'], frontend)
            deps_stamp.write_text(lock_hash)
        print('[2/4] Building dashboard...', flush=True)
        run([npm, 'run', 'build'], frontend)
        stamp.write_text(source_hash)
    else:
        print('[2/4] Dashboard is up to date.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--smoke-test', action='store_true', help='Check health and dashboard, then stop the owned server.')
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    # Held for the whole process; released by Windows even after an abrupt exit.
    import msvcrt
    with (WORK / 'launch.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print('The launcher is already running. Use its window or stop it with Ctrl+C first.')
            return 1
        prepare()
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', 8000))
            except OSError:
                sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        # Separate generated files from manually started servers; respect LLM config.
        env = dict(os.environ, PYTHONUTF8='1', VAR_DIR=str(WORK / 'runtime'), INDEX_PATH=str(WORK / 'index.json'))
        print('[3/4] Rebuilding data and knowledge index...', flush=True)
        subprocess.run([str(PYTHON), '-m', 'kbqa.rebuild'], cwd=ROOT / 'starter', env=env, check=True)
        process = None
        with (WORK / 'server.log').open('w', encoding='utf-8') as log:
            try:
                process = subprocess.Popen([str(PYTHON), '-m', 'uvicorn', 'kbqa.server:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT / 'starter', env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f'Server exited. See {WORK / "server.log"}')
                    try:
                        with opener.open(url + '/api/health', timeout=2) as response:
                            health = json.load(response)
                        if health.get('status') == 'ok':
                            break
                    except (OSError, ValueError):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'Startup timed out. See {WORK / "server.log"}')
                    time.sleep(0.3)
                with opener.open(url + '/', timeout=5) as response:
                    if response.status != 200 or b'<html' not in response.read().lower():
                        raise RuntimeError('Dashboard did not load correctly.')
                print(f'[4/4] Ready: {url}/  (LLM: {health.get("llm_mode")})', flush=True)
                print('Keep this window open. Press Ctrl+C to stop. Logs: ' + str(WORK / 'server.log'), flush=True)
                if args.smoke_test:
                    print('SMOKE TEST PASSED: health + dashboard HTTP 200', flush=True)
                    return 0
                if not args.no_browser:
                    webbrowser.open(url + '/')
                return process.wait()
            finally:
                if process is not None and process.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    process.wait(timeout=10)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nProject stopped.')
        raise SystemExit(0)
    except Exception as exc:
        print(f'\nSTART FAILED: {exc}', file=sys.stderr)
        raise SystemExit(1)
