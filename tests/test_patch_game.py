"""Windows fixture tests. All writes stay in --work; --source is read-only."""

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
ENTRY = REPOSITORY / 'patch_game.py'
PACKAGE = REPOSITORY / 'patch'
SPEC = importlib.util.spec_from_file_location('fixture_patch_game', ENTRY)
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


def hashes(directory, manifest):
    return {item['relative_path']: PATCHER.file_hash(directory / item['relative_path'])
            for item in manifest['files']}


def negative_manifest_checks(work):
    """Use tiny manifests to exercise validation without touching asset fixtures."""
    baseline = {
        'format_version': 1, 'game': "Yog-Sothoth's Yard", 'steam_build': '24329824',
        'payload_size': 4, 'payload_sha256': hashlib.sha256(b'BBBB').hexdigest(),
        'files': [{
            'relative_path': relative, 'original_size': 4, 'patched_size': 4,
            'original_sha256': hashlib.sha256(b'AAAA').hexdigest(),
            'patched_sha256': hashlib.sha256(b'BBBB').hexdigest(),
            'operations': [{'kind': 'add', 'offset': 0, 'length': 4}],
        } for relative in sorted(PATCHER.ALLOWED_PATHS)],
    }
    folder = work / 'manifest-cases'
    folder.mkdir()
    path = folder / 'patch_manifest.json'
    path.write_text(json.dumps(baseline), encoding='utf-8')
    PATCHER.load_manifest(folder)
    mutations = []
    for key, value in [('format_version', True), ('format_version', 2),
                       ('steam_build', 'unknown'), ('payload_size', True),
                       ('payload_size', -1), ('payload_sha256', 'not-a-hash')]:
        data = copy.deepcopy(baseline)
        data[key] = value
        mutations.append((str(key) + '-' + str(value), data))
    for relative in ('../outside.assets', 'C:/outside.assets', '//server/share/file',
                     "Yog-Sothoth's Yard_Data/../resources.assets", False,
                     "Yog-Sothoth's Yard_Data\\resources.assets"):
        data = copy.deepcopy(baseline)
        data['files'][0]['relative_path'] = relative
        mutations.append(('invalid-path-' + str(relative), data))
    data = copy.deepcopy(baseline)
    data['files'][1] = copy.deepcopy(data['files'][0])
    mutations.append(('duplicate-target', data))
    data = copy.deepcopy(baseline)
    data['files'].pop()
    mutations.append(('missing-target', data))
    for field, value in [('kind', 'execute'), ('offset', -1), ('offset', True),
                         ('offset', 4), ('length', 0), ('length', True), ('length', 5)]:
        data = copy.deepcopy(baseline)
        data['files'][0]['operations'][0][field] = value
        mutations.append(('invalid-operation-' + field + '-' + str(value), data))
    for field, value in [('original_size', True), ('patched_size', -1),
                         ('patched_size', 5), ('patched_sha256', 'bad')]:
        data = copy.deepcopy(baseline)
        data['files'][0][field] = value
        mutations.append(('invalid-file-' + field + '-' + str(value), data))
    mutations.append(('top-level-list', []))
    results = []
    for name, data in mutations:
        path.write_text(json.dumps(data), encoding='utf-8')
        try:
            PATCHER.load_manifest(folder)
        except (PATCHER.PatchError, ValueError, TypeError):
            results.append({'name': name, 'passed': True})
        else:
            raise AssertionError('Malformed manifest accepted: ' + name)
    path.write_text('{', encoding='utf-8')
    try:
        PATCHER.load_manifest(folder)
    except ValueError:
        results.append({'name': 'truncated-json', 'passed': True})
    else:
        raise AssertionError('Truncated JSON accepted')
    return results


def run(source, work, report_path):
    if sys.platform != 'win32':
        raise RuntimeError('These tests require Windows; the installer uses Win32 replacement/locking')
    source = Path(source).resolve(strict=True)
    work = Path(work).resolve()
    if (work == source or work.is_relative_to(source) or source.is_relative_to(work)
            or work == REPOSITORY or REPOSITORY.is_relative_to(work)):
        raise ValueError('--work must be separate from --source and must not contain the repository')
    work.mkdir(parents=True, exist_ok=True)
    run_root = work / ('patch-fixture-' + uuid.uuid4().hex[:10])
    fixture = run_root / 'Тест игры & путь'
    fixture.mkdir(parents=True)
    manifest = PATCHER.load_manifest(PACKAGE)
    original = {item['relative_path']: item['original_sha256'] for item in manifest['files']}
    patched = {item['relative_path']: item['patched_sha256'] for item in manifest['files']}
    if hashes(source, manifest) != original:
        raise ValueError('--source does not contain the supported original assets')
    for name in original:
        target = fixture / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    (fixture / PATCHER.GAME_EXE).write_bytes(b'MZ-fixture-only-never-executed')
    report = {'status': 'running', 'fixture': str(fixture), 'steps': [], 'manifest_cases': [],
              'entry_sha256': PATCHER.file_hash(ENTRY), 'source_files_written': False,
              'real_game_executable_copied_or_executed': False}

    def record(name, action, expected, success=True, entry=ENTRY):
        mtimes = {key: (fixture / key).stat().st_mtime_ns for key in original}
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', str(entry), action, '--game', str(fixture), '--non-interactive'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', errors='replace', timeout=240,
        )
        actual = hashes(fixture, manifest)
        passed = (result.returncode == 0) == success and actual == expected
        if not success or 'idempotent' in name:
            passed = passed and all((fixture / key).stat().st_mtime_ns == mtimes[key] for key in original)
        report['steps'].append({'name': name, 'passed': passed, 'exit_code': result.returncode,
                                'elapsed_seconds': round(time.monotonic() - started, 3),
                                'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()})
        print(json.dumps({'name': name, 'passed': passed}), flush=True)
        if not passed:
            raise AssertionError((name, result.returncode, result.stdout, result.stderr, actual))

    def check_backups():
        actual = {name: PATCHER.file_hash(fixture / PATCHER.BACKUP_DIR / (Path(name).name + '.original'))
                  for name in original}
        if actual != original:
            raise AssertionError('Original backup hashes differ')

    try:
        record('verify_originals', 'verify', original)
        record('install', 'install', patched)
        check_backups()
        record('idempotent_install', 'install', patched)
        record('verify_installed', 'verify', patched)
        record('restore', 'restore', original)
        record('idempotent_restore', 'restore', original)
        record('verify_restored', 'verify', original)
        first_name = next(iter(original))
        first = fixture / first_name
        with first.open('r+b') as stream:
            byte = stream.read(1)
            stream.seek(0)
            stream.write(bytes([byte[0] ^ 1]))
        record('reject_unsupported_input', 'install', hashes(fixture, manifest), success=False)
        shutil.copy2(source / first_name, first)
        bad = run_root / 'corrupt-package'
        (bad / 'patch').mkdir(parents=True)
        shutil.copy2(ENTRY, bad / ENTRY.name)
        for filename in ('patch_manifest.json', 'payload.bin'):
            shutil.copy2(PACKAGE / filename, bad / 'patch' / filename)
        with (bad / 'patch/payload.bin').open('r+b') as stream:
            stream.seek(-1, 2)
            byte = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([byte[0] ^ 1]))
        record('reject_corrupted_payload', 'install', original, success=False, entry=bad / ENTRY.name)
        report['manifest_cases'] = negative_manifest_checks(run_root)
        calls = []

        def fail_second_replace(pending, target, rollback=None):
            calls.append(str(target))
            if len(calls) == 2:
                raise OSError('Injected second-file commit failure')
            PATCHER.replace_game_file(pending, target, rollback)

        for action, wanted in [('install', original), ('restore', patched)]:
            if action == 'restore':
                PATCHER.run_patch('install', fixture)
            calls.clear()
            try:
                PATCHER.run_patch(action, fixture, replace_file=fail_second_replace)
            except OSError:
                pass
            else:
                raise AssertionError('Injected commit failure was not propagated')
            if len(calls) != 2 or hashes(fixture, manifest) != wanted:
                raise AssertionError('Failed second-file commit did not restore both prior files')
            check_backups()
            if list(fixture.rglob('*.yog_ru_pending')) or list(fixture.rglob('*.yog_ru_rollback')):
                raise AssertionError('Rollback left pending or rollback files')
            report['steps'].append({'name': action + '_second_commit_rollback', 'passed': True})
        PATCHER.run_patch('restore', fixture)
        original_unlink = Path.unlink
        errors = io.StringIO()

        def fail_rollback_cleanup(path, *args, **kwargs):
            if path.name.endswith('.yog_ru_rollback'):
                raise OSError('Injected post-commit cleanup failure')
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', fail_rollback_cleanup), contextlib.redirect_stderr(errors):
            PATCHER.run_patch('install', fixture)
        if hashes(fixture, manifest) != patched or not errors.getvalue():
            raise AssertionError('Cleanup warning caused partial rollback or was not reported')
        for rollback in fixture.rglob('*.yog_ru_rollback'):
            original_unlink(rollback)
        report['steps'].append({'name': 'post_commit_cleanup_failure_keeps_both_patched', 'passed': True})
        PATCHER.run_patch('restore', fixture)
        check_backups()
        if hashes(source, manifest) != original or hashes(fixture, manifest) != original:
            raise AssertionError('Source input changed or final fixture was not restored')
        report['status'] = 'passed'
    except BaseException as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        destination = Path(report_path).resolve() if report_path else run_root / 'test_report.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='read-only directory with original game assets')
    parser.add_argument('--work', type=Path, required=True, help='separate directory for disposable fixture copies')
    parser.add_argument('--report', type=Path, help='optional JSON report destination')
    arguments = parser.parse_args()
    run(arguments.source, arguments.work, arguments.report)
