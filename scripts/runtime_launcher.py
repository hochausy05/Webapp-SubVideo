"""Supervise the local AutoDub services on Windows."""
from __future__ import annotations

import os
import ctypes
from ctypes import wintypes
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
FRONTEND = ROOT / 'frontend'
PYTHON = BACKEND / '.venv' / 'Scripts' / 'python.exe'
STOP = threading.Event()


@dataclass
class Service:
    name: str
    command: list[str]
    cwd: Path
    ready_marker: str | None = None
    process: subprocess.Popen[str] | None = None
    ready: threading.Event | None = None
    restarts: int = 0
    job_handle: int | None = None


class _BasicLimit(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


def create_process_job(process: subprocess.Popen[str]) -> int:
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limit = _ExtendedLimit()
    limit.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limit), ctypes.sizeof(limit)) or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ctypes.WinError(error)
    return int(handle)


def close_process_job(service: Service) -> None:
    if service.job_handle is not None:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(wintypes.HANDLE(service.job_handle))
        service.job_handle = None


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def existing_workers() -> list[str]:
    # Inspect command lines locally; only return service names, never process details.
    query = ("$p=Get-CimInstance Win32_Process | Where-Object { "
             "$_.Name -match '^python(w)?\\.exe$' -and "
             "$_.CommandLine -match '-m\\s+app\\.workers\\.(transcription_worker|translation_worker|tts_worker)' }; "
             "$p | ForEach-Object { if ($_.CommandLine -match 'transcription_worker') { 'ASR' } "
             "elseif ($_.CommandLine -match 'translation_worker') { 'TRANSLATION' } "
             "elseif ($_.CommandLine -match 'tts_worker') { 'TTS' } } | Sort-Object -Unique")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', query],
        capture_output=True, text=True, timeout=15, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    return [line.strip() for line in result.stdout.splitlines() if line.strip() in {'ASR', 'TRANSLATION', 'TTS'}]


def start(service: Service, events: queue.Queue[tuple[str, str]]) -> None:
    service.ready = threading.Event()
    environment = os.environ.copy()
    environment['PYTHONUNBUFFERED'] = '1'
    environment['PYTHONIOENCODING'] = 'utf-8'
    service.process = subprocess.Popen(service.command, cwd=service.cwd, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', bufsize=1,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    process = service.process
    try:
        service.job_handle = create_process_job(process)
    except OSError:
        process.terminate()
        process.wait(timeout=5)
        raise
    print(f'[{service.name}] started PID {process.pid}', flush=True)

    def relay() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if service.ready_marker and service.ready_marker in line:
                assert service.ready is not None
                service.ready.set()
            if line:
                print(f'[{service.name}] {line}', flush=True)
        events.put((service.name, 'exited'))

    threading.Thread(target=relay, name=f'log-{service.name}', daemon=True).start()


def stop_service(service: Service) -> None:
    process = service.process
    if process is None:
        return
    if process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            # Only the process tree started by this supervisor is targeted.
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=15)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    close_process_job(service)
    if process.stdout:
        process.stdout.close()


def wait_http(service: Service, url: str, timeout: int = 40) -> None:
    deadline = time.monotonic() + timeout
    while not STOP.is_set() and time.monotonic() < deadline:
        if service.process and service.process.poll() is not None:
            raise RuntimeError(f'{service.name} exited during startup')
        if healthy(url):
            return
        STOP.wait(0.5)
    raise RuntimeError(f'{service.name} did not become ready at {url}')


def wait_worker(service: Service, timeout: int = 45) -> None:
    deadline = time.monotonic() + timeout
    while not STOP.is_set() and time.monotonic() < deadline:
        if service.process and service.process.poll() is not None:
            raise RuntimeError(f'{service.name} exited before connecting to Supabase')
        if service.ready and service.ready.wait(0.25):
            return
    raise RuntimeError(f'{service.name} did not confirm its Job RPC connection')


def main() -> int:
    if os.name != 'nt':
        print('[ERROR] This launcher requires Windows.', flush=True)
        return 1
    if not PYTHON.is_file():
        print('[ERROR] backend/.venv is missing; follow README setup.', flush=True)
        return 1
    npm = shutil.which('npm.cmd')
    node = shutil.which('node.exe')
    if not npm or not node:
        print('[ERROR] Node.js/npm not found in PATH; install Node.js and follow README setup.', flush=True)
        return 1
    npm_cli = Path(npm).parent / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js'
    if not npm_cli.is_file():
        print('[ERROR] npm CLI was not found beside npm.cmd; reinstall Node.js/npm.', flush=True)
        return 1
    if not (FRONTEND / 'node_modules' / 'vite' / 'bin' / 'vite.js').is_file():
        print('[ERROR] frontend dependencies are missing; follow README setup.', flush=True)
        return 1
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        print('[ERROR] Backend dependencies are missing; install backend/requirements.txt.', flush=True)
        return 1
    lock_path = BACKEND / '.cache' / 'runtime-launcher.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print('[ERROR] AutoDub launcher is already running.', flush=True)
        return 1
    services: list[Service] = []
    events: queue.Queue[tuple[str, str]] = queue.Queue()
    try:
        for port in (8000, 5173):
            if not port_available(port):
                raise RuntimeError(f'Port {port} is already in use; stop the existing service before launching.')
        workers = existing_workers()
        if workers:
            raise RuntimeError(f'Existing worker(s) detected: {", ".join(workers)}. Stop them before launching.')
        services = [
            Service('BACKEND', [str(PYTHON), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000'], BACKEND),
            Service('FRONTEND', [node, str(npm_cli), 'run', 'dev', '--', '--host', '127.0.0.1', '--port', '5173', '--strictPort'], FRONTEND),
            Service('ASR', [str(PYTHON), '-m', 'app.workers.transcription_worker'], BACKEND, 'RUNTIME_READY kind=transcription'),
            Service('TRANSLATION', [str(PYTHON), '-m', 'app.workers.translation_worker'], BACKEND, 'RUNTIME_READY kind=translation'),
            Service('TTS', [str(PYTHON), '-m', 'app.workers.tts_worker'], BACKEND, 'RUNTIME_READY kind=tts'),
        ]
        for index, service in enumerate(services):
            if STOP.is_set():
                return 0
            start(service, events)
            if index == 0:
                wait_http(service, 'http://127.0.0.1:8000/health')
            elif index == 1:
                wait_http(service, 'http://127.0.0.1:5173/')
            else:
                wait_worker(service)
            print(f'[OK] {service.name} ready', flush=True)
        print('\nAutoDub: http://127.0.0.1:5173\nPress Ctrl+C to stop AutoDub.\n', flush=True)
        while not STOP.wait(0.5):
            for service in services:
                if service.process is None or service.process.poll() is None:
                    continue
                if service.name in ('BACKEND', 'FRONTEND'):
                    raise RuntimeError(f'{service.name} stopped (exit {service.process.returncode}); shutting down.')
                if service.restarts >= 2:
                    raise RuntimeError(f'{service.name} stopped repeatedly; shutting down.')
                service.restarts += 1
                close_process_job(service)
                print(f'[WARN] {service.name} stopped; restart {service.restarts}/2 in {service.restarts * 3}s', flush=True)
                if STOP.wait(service.restarts * 3):
                    break
                start(service, events)
                wait_worker(service)
                print(f'[OK] {service.name} reconnected', flush=True)
        return 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f'[ERROR] {exc}', flush=True)
        return 1
    finally:
        STOP.set()
        for service in reversed(services):
            stop_service(service)
        lock.release()
        print('[INFO] AutoDub supervisor stopped.', flush=True)


if __name__ == '__main__':
    def request_stop(_signum: int, _frame: object) -> None:
        STOP.set()
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, request_stop)
    raise SystemExit(main())
