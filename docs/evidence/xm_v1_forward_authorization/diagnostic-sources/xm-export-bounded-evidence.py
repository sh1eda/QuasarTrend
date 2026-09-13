"""Read-only evidence export; failed or incomplete tests do not block preservation.

No tests, native history requests, services, capture, orders, or gate changes.
"""
from __future__ import annotations
import argparse
from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

COLLECTOR_SHA = '721975200ea6458aa0662c0bf99cafe417fc5de46ba75f62d8b8fd5763012baa'
SOURCES = (
    'tests/test_forward_capture_integrity.py', 'tests/test_persistence.py',
    'tools/validate_xm_forward_recovery.py', 'tests/test_forward_validation_evidence.py',
    'tools/probe_xm_forward_history.py', 'tests/test_forward_history_probe.py',
    'tests/test_forward_authorization_blockers.py',
    'tests/golden/tradingview_15m.csv', 'tests/golden/tradingview_4h.csv',
    'exports/xauusd_pending/XAUUSD_15m.csv', 'exports/xauusd_pending/XAUUSD_4h.csv',
)
ALLOWLIST = tuple(f'{directory}/{name}'
                  for directory in ('evidence', 'evidence-with-pytest', 'final263run', 'final263-ownerwait-v2')
                  for name in ('evidence.json', 'junit.xml', 'pytest.txt')) + (
    'lock-owner-observation-v1/evidence.json',
    'native-observation-v1/evidence.json', 'native-observation-v1/preflight_failure.json',
    'diagnostic22.xml', 'diagnostic22.log', 'diagnostic22.json', 'finish-validation.json', 'native-wrapper.log',
)
PRIVATE_KEYS = frozenset(('login', 'password', 'passwd', 'secret', 'token', 'access_token', 'refresh_token', 'api_key'))


def digest(data):
    return sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def normalize(data, suffix, replacements):
    text = data.decode('utf-8-sig')
    for path, marker in replacements:
        # Covers normal JSON escaped backslashes, plain Windows paths, and / paths.
        for spelling in (path.replace('\\', '\\\\'), path, path.replace('\\', '/')):
            text = text.replace(spelling, marker)
    if suffix == '.json':
        def scrub(value):
            if isinstance(value, dict):
                return {key: '[REDACTED]' if key.lower() in PRIVATE_KEYS else scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        return encoded(scrub(json.loads(text)))
    return text.encode('utf-8')


def source_snapshot(repo):
    result = {'files_sha256': {name: digest((repo / name).read_bytes()) for name in SOURCES if (repo / name).is_file()},
              'missing_source_paths': [name for name in SOURCES if not (repo / name).is_file()]}
    collector_path = repo / 'tools/validate_xm_forward_recovery.py'
    if digest(collector_path.read_bytes()) != COLLECTOR_SHA:
        result['runtime_provenance_status'] = 'REVIEWED_COLLECTOR_MISMATCH'
        return result
    spec = importlib.util.spec_from_file_location('reviewed_export_collector', collector_path)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    result['source_identity'] = collector.source_identity(repo)
    result['packages'] = {name: collector.package_version(name) for name in ('MetaTrader5', 'numpy', 'pytest', 'quasartrend')}
    result['runtime_provenance_status'] = 'OBSERVED'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--stage', type=Path, required=True)
    args = parser.parse_args(argv)
    repo, stage = args.repo.resolve(), args.stage.resolve()
    if platform.system() != 'Windows' or not repo.is_dir() or not stage.is_dir() or stage.is_relative_to(repo):
        parser.error('existing Windows checkout and external stage required')
    desktop_result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', "[Environment]::GetFolderPath('Desktop')"], capture_output=True, text=True, check=True, timeout=10)
    desktop = Path(desktop_result.stdout.strip())
    if not desktop.is_dir():
        parser.error('Desktop directory unavailable')
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    destination = desktop / f'xm-bounded-evidence-{stamp}.zip'
    replacements = [(str(repo), '[REPO]'), (str(stage), '[STAGE]'), (str(Path.home()), '[HOME]')]
    metadata = {'schema': 'xm-bounded-evidence-export/v1', 'started_utc': datetime.now(UTC).isoformat(),
                'execution': False, 'capture': False, 'authorization': 'BLOCKED',
                'acceptance_decision': 'NOT_PERFORMED', 'collector_pass_required_for_export': False,
                'exporter_sha256': digest(Path(__file__).read_bytes()),
                'python_version': platform.python_version(), 'platform': platform.platform(),
                'python_executable_sha256': digest(Path(sys.executable).read_bytes()),
                'python_executable_path_sha256': digest(str(Path(sys.executable).resolve()).encode()),
                'normalization': 'UTF-8; known workstation path prefixes replaced; JSON canonicalized; explicitly private JSON keys redacted. Source files unchanged. Manifest records original and exported SHA256 separately.',
                'missing_evidence': [], 'unreadable_evidence': [], 'changed_during_export': []}
    try:
        metadata['before'] = source_snapshot(repo)
    except Exception as error:
        metadata['source_snapshot_failure_type'] = type(error).__name__
    blobs, originals = {}, {}
    for name in ALLOWLIST:
        path = stage / name
        if not path.is_file():
            metadata['missing_evidence'].append(name)
            continue
        try:
            if not path.resolve().is_relative_to(stage):
                raise ValueError('evidence path escapes stage')
            original = path.read_bytes()
            blobs[name] = normalize(original, path.suffix, replacements)
            originals[name] = {'original_sha256': digest(original), 'original_bytes': len(original)}
        except Exception as error:
            metadata['unreadable_evidence'].append({'path': name, 'failure_type': type(error).__name__})
    # Retain partial/incomplete evidence but explicitly record changes during read.
    for name, identity in originals.items():
        try:
            if digest((stage / name).read_bytes()) != identity['original_sha256']:
                metadata['changed_during_export'].append(name)
        except Exception:
            metadata['changed_during_export'].append(name)
    try:
        metadata['after'] = source_snapshot(repo)
        metadata['source_snapshot_unchanged'] = metadata.get('before') == metadata['after']
    except Exception as error:
        metadata['final_source_snapshot_failure_type'] = type(error).__name__
        metadata['source_snapshot_unchanged'] = False
    metadata['finished_utc'] = datetime.now(UTC).isoformat()
    blobs['export-metadata.json'] = normalize(encoded(metadata), '.json', replacements)
    manifest = {'schema': 'xm-evidence-export-manifest/v1', 'members': {
        name: {**originals.get(name, {}), 'exported_sha256': digest(data), 'exported_bytes': len(data)} for name, data in blobs.items()}}
    with zipfile.ZipFile(destination, mode='x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in blobs.items():
            archive.writestr(name, data)
        archive.writestr('manifest.json', encoded(manifest))
    print(json.dumps({'export_name': destination.name, 'zip_sha256': digest(destination.read_bytes()), 'members': len(blobs) + 1,
                      'execution': False, 'capture': False, 'authorization': 'BLOCKED'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
