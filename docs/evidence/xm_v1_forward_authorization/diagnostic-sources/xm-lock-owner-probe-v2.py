"""Bounded synthetic Windows lock-owner observation; no terminal/broker APIs."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

CHILD = '''from pathlib import Path
import os, sys, json
from hashlib import sha256
from quasartrend.forward import durable
lock = durable.EvidenceLock(Path(sys.argv[1]))
print(json.dumps({'pid': os.getpid(), 'durable_path': str(Path(durable.__file__).resolve()), 'durable_sha256': sha256(Path(durable.__file__).read_bytes()).hexdigest(), 'python_executable_sha256': sha256(Path(sys.executable).read_bytes()).hexdigest()}), flush=True)
input()
lock.close()
'''

WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258


def now():
    return datetime.now(UTC).isoformat()


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repo', type=Path, required=True)
    args = parser.parse_args(argv)
    repo, output = args.repo.resolve(), args.output.resolve()
    if sys.platform != 'win32' or output.is_relative_to(repo):
        parser.error('Windows and external output required')
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    def remaining(maximum):
        return max(0.001, min(maximum, 55 - (time.monotonic() - started)))
    report = {'schema': 'xm-windows-lock-owner-probe/v1', 'started_utc': now(), 'cases': [],
              'execution': False, 'capture': False, 'status': 'INCOMPLETE',
              'python_version': sys.version, 'python_executable_sha256': digest(sys.executable),
              'python_executable_path_sha256': sha256(str(Path(sys.executable).resolve()).encode()).hexdigest(),
              'probe_sha256': digest(__file__)}
    def save():
        pending = output / 'evidence.pending'
        pending.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        pending.replace(output / 'evidence.json')
    save()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    try:
        from quasartrend.forward import durable
        module = Path(durable.__file__).resolve()
        if module != repo / 'src/quasartrend/forward/durable.py':
            raise ValueError('unexpected durable import')
        report['durable_source_sha256'] = digest(module)
        report['child_source_sha256'] = sha256(CHILD.encode()).hexdigest()
        for label, original in (('empty', b''), ('nonempty', b'lock-sentinel')):
            root = output / label
            root.mkdir()
            lock_path = root / '.xm-forward.lock'
            lock_path.write_bytes(original)
            (root / 'evidence').write_bytes(b'preserve')
            stat = lock_path.stat()
            lock_identity = (stat.st_dev, stat.st_ino, stat.st_size)
            case = {'label': label, 'started_utc': now(), 'status': 'INCOMPLETE', 'lock_initial_sha256': digest(lock_path), 'attempts': []}
            report['cases'].append(case)
            child = None
            owner_handle = None
            reader = None
            def lock_attempt(phase):
                stamp = time.monotonic()
                result = {'phase': phase, 'utc': now()}
                try:
                    acquired = durable.EvidenceLock(root)
                    acquired.close()
                    result['outcome'] = 'ACQUIRED_AND_RELEASED'
                except PermissionError:
                    result['outcome'] = 'DENIED'
                except Exception as error:
                    result['outcome'] = 'ERROR'
                    result['failure_type'] = type(error).__name__
                result['elapsed_seconds'] = time.monotonic() - stamp
                case['attempts'].append(result)
                save()
                return result['outcome']
            try:
                child = subprocess.Popen([sys.executable, '-c', CHILD, str(root)], cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                case['popen_pid'] = child.pid
                lines = queue.Queue()
                def read_pid():
                    try:
                        lines.put(child.stdout.readline())
                    except Exception:
                        lines.put('')
                reader = threading.Thread(target=read_pid, daemon=True)
                reader.start()
                line = lines.get(timeout=remaining(8)).strip()
                handshake = json.loads(line)
                owner_pid = handshake.get('pid')
                if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
                    raise ValueError('owner PID not observed')
                case['child_durable_path_matches'] = Path(handshake['durable_path']).resolve() == module
                case['child_durable_sha256'] = handshake['durable_sha256']
                case['child_python_executable_sha256'] = handshake['python_executable_sha256']
                if not (case['child_durable_path_matches'] and case['child_durable_sha256'] == report['durable_source_sha256'] and case['child_python_executable_sha256'] == report['python_executable_sha256']):
                    raise ValueError('child source or interpreter mismatch')
                case['owner_pid'] = owner_pid
                case['owner_differs_from_popen'] = owner_pid != child.pid
                # This handle binds the exact process that just proved acquisition,
                # and is opened before terminating the launcher, avoiding PID reuse.
                owner_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, owner_pid)
                if not owner_handle:
                    raise OSError('owner process handle unavailable')
                case['owner_before_kill_wait0'] = int(kernel.WaitForSingleObject(owner_handle, 0))
                if case['owner_before_kill_wait0'] != WAIT_TIMEOUT or lock_attempt('while_owner_holds') != 'DENIED':
                    raise ValueError('live exclusive owner not proven')
                case['kill_launcher_utc'] = now()
                child.kill()
                case['launcher_returncode'] = child.wait(timeout=remaining(10))
                case['launcher_wait_returned_utc'] = now()
                case['owner_after_launcher_wait0'] = int(kernel.WaitForSingleObject(owner_handle, 0))
                lock_attempt('immediate_after_launcher_wait')
                waited = time.monotonic()
                case['owner_wait10000'] = int(kernel.WaitForSingleObject(owner_handle, int(remaining(10) * 1000)))
                case['owner_wait_elapsed_seconds'] = time.monotonic() - waited
                lock_attempt('after_bounded_owner_wait')
                case['status'] = 'OBSERVED'
            except Exception as error:
                case['failure_type'] = type(error).__name__
            finally:
                # Terminate only our own child/owner, through its still-open handle.
                if owner_handle:
                    state = int(kernel.WaitForSingleObject(owner_handle, 0))
                    case['cleanup_owner_wait0'] = state
                    if state == WAIT_TIMEOUT:
                        case['cleanup_owner_terminated'] = bool(kernel.TerminateProcess(owner_handle, 1))
                        case['cleanup_owner_wait3000'] = int(kernel.WaitForSingleObject(owner_handle, int(remaining(3) * 1000)))
                    case['owner_final_wait0'] = int(kernel.WaitForSingleObject(owner_handle, 0))
                    kernel.CloseHandle(owner_handle)
                if child is not None:
                    if child.poll() is None:
                        try:
                            child.kill()
                            child.wait(timeout=remaining(3))
                        except Exception as error:
                            case['cleanup_launcher_failure_type'] = type(error).__name__
                    if child.stdin is not None:
                        try:
                            child.stdin.close()
                        except Exception as error:
                            case['cleanup_stdin_failure_type'] = type(error).__name__
                    if child.stdout is not None and (reader is None or not reader.is_alive()):
                        try:
                            child.stdout.close()
                        except Exception as error:
                            case['cleanup_stdout_failure_type'] = type(error).__name__
                    elif reader is not None and reader.is_alive():
                        case['cleanup_stdout_reader_unresolved'] = True
                try:
                    current = lock_path.stat()
                    case['lock_identity_unchanged'] = (current.st_dev, current.st_ino, current.st_size) == lock_identity
                    case['evidence_bytes_unchanged'] = (root / 'evidence').read_bytes() == b'preserve'
                    case['lock_bytes_unchanged_after_cleanup'] = lock_path.read_bytes() == original
                except Exception as error:
                    case['post_cleanup_failure_type'] = type(error).__name__
                    case['status'] = 'INCOMPLETE'
                case['finished_utc'] = now()
                save()
            if time.monotonic() - started > 50:
                break
        report['durable_source_unchanged'] = digest(module) == report['durable_source_sha256']
        if (len(report['cases']) == 2 and report['durable_source_unchanged']
                and all(case['status'] == 'OBSERVED' and case.get('owner_final_wait0') == WAIT_OBJECT_0 and not any(key.startswith('cleanup_') and (key.endswith('_failure_type') or key.endswith('_unresolved')) for key in case) and case.get('lock_identity_unchanged') and case.get('evidence_bytes_unchanged') and case.get('lock_bytes_unchanged_after_cleanup') for case in report['cases'])):
            report['status'] = 'TWO_CASES_OBSERVED_NO_CAUSAL_VERDICT'
    except Exception as error:
        report['failure_type'] = type(error).__name__
    report['finished_utc'] = now()
    report['elapsed_seconds'] = time.monotonic() - started
    save()
    print(json.dumps({'status': report['status'], 'execution': False, 'capture': False}))
    return 0 if report['status'] == 'TWO_CASES_OBSERVED_NO_CAUSAL_VERDICT' else 2


if __name__ == '__main__':
    raise SystemExit(main())
