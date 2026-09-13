"""Two synthetic diagnostic test files, bounded to 120 seconds; no native calls."""
import argparse
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

EXPECTED = {
    'tools/validate_xm_forward_recovery.py': '721975200ea6458aa0662c0bf99cafe417fc5de46ba75f62d8b8fd5763012baa',
    'tests/test_forward_validation_evidence.py': 'de16920bc0964610064f8b2a3627c27829995ca196356f33fc069bedde31f722',
    'tools/probe_xm_forward_history.py': '87657ab68b5283c716231643e4dfefe33fe0f5017728201d9772f36fbad5f2ab',
    'tests/test_forward_history_probe.py': '655b1f5797c2dec5057d7987c44d96c16d4365ca8584fe2528e8cd65f919fcf6',
}
CHILD = """from pathlib import Path
import sys
import quasartrend.forward.mt5 as forward
root = (Path(sys.argv[1]) / 'src').resolve()
for name, module in tuple(sys.modules.items()):
    if name == 'quasartrend' or name.startswith('quasartrend.'):
        source = getattr(module, '__file__', None)
        if not source or not Path(source).resolve().is_relative_to(root):
            raise RuntimeError('wrong runtime source')
if forward.FORWARD_CAPTURE_INTEGRITY_AUTHORIZED is not False:
    raise RuntimeError('capture gate enabled')
import pytest
raise SystemExit(pytest.main(sys.argv[2:]))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--stage', type=Path, required=True)
    args = parser.parse_args()
    repo, stage = args.repo.resolve(), args.stage.resolve()
    paths = [stage / ('diagnostic22.' + suffix) for suffix in ('xml', 'log', 'json')]
    if not repo.is_dir() or not stage.is_dir() or stage.is_relative_to(repo) or any(p.exists() for p in paths):
        parser.error('existing checkout/external stage and fresh diagnostic22 paths required')
    xml, log, metadata = paths
    report = {'status': 'INCOMPLETE', 'execution': False, 'capture': False, 'authorization': False}
    collector = None
    digest = lambda path: sha256(path.read_bytes()).hexdigest()
    def identity():
        return {'source': collector.source_identity(repo), 'diagnostics': {name: digest(repo / name) for name in EXPECTED}}
    def clean(value):
        text = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')
        for path, label in ((repo, '[REPO]'), (stage, '[STAGE]'), (Path.home(), '[HOME]')):
            text = text.replace(str(path), label).replace(str(path).replace('\\', '/'), label)
        return text
    try:
        if {name: digest(repo / name) for name in EXPECTED} != EXPECTED:
            raise ValueError('reviewed diagnostic hash mismatch')
        spec = importlib.util.spec_from_file_location('reviewed_collector22', repo / 'tools/validate_xm_forward_recovery.py')
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        report['before'] = identity()
        tests = ['tests/test_forward_history_probe.py', 'tests/test_forward_validation_evidence.py']
        try:
            result = subprocess.run([sys.executable, '-c', CHILD, str(repo), *tests, '-q', '--tb=short', f'--junitxml={xml}'], cwd=repo, capture_output=True, text=True, timeout=120)
            report['returncode'] = result.returncode
            log.write_text(clean(result.stdout) + clean(result.stderr), encoding='utf-8')
        except subprocess.TimeoutExpired as error:
            report['timeout'] = True
            log.write_text(clean(error.stdout) + clean(error.stderr), encoding='utf-8')
            raise
        cases = list(ET.parse(xml).getroot().iter('testcase'))
        report.update(tests=len(cases), failed=sum(c.find('failure') is not None or c.find('error') is not None for c in cases), skipped=sum(c.find('skipped') is not None for c in cases), junit_sha256=digest(xml), junit_normalization='NONE_ORIGINAL_BYTES')
        if result.returncode == 0 and report['tests'] == 22 and report['failed'] == report['skipped'] == 0:
            report['status'] = '22_SYNTHETIC_TESTS_PASSED'
    except Exception as error:
        report['failure_type'] = type(error).__name__
    finally:
        if collector is not None:
            try:
                report['after'] = identity()
                report['source_unchanged'] = report.get('before') == report['after']
                if not report['source_unchanged']:
                    report['status'] = 'INCOMPLETE'
            except Exception as error:
                report['final_identity_failure_type'] = type(error).__name__
                report['status'] = 'INCOMPLETE'
        metadata.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'execution', 'capture', 'authorization')}))
    return 0 if report['status'] == '22_SYNTHETIC_TESTS_PASSED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
