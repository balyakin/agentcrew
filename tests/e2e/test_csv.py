"""CSV acceptance: real Git/SQLite/tmux/checks, deterministic provider only in tests."""

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentcrew.config import load_config
from agentcrew.db import Database
from agentcrew.develop.scaffold import scaffold
from agentcrew.develop.workflow import start_develop
from agentcrew.git_snapshot import apply_candidate
from agentcrew.models import UserError
from agentcrew.tmux_backend import Tmux

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'csv'


def command(argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=10, check=False)


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=10, check=True).stdout


@pytest.fixture
def provider(tmp_path, monkeypatch):
    directory = tmp_path / 'bin'
    directory.mkdir()
    executable = directory / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + (FIXTURES / 'provider.py').read_text())
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(directory) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('CSV_FIXTURES', str(FIXTURES))
    calls = tmp_path / 'calls.jsonl'
    monkeypatch.setenv('CSV_CALLS', str(calls))
    return calls


def run_csv(tmp_path, monkeypatch, provider, template, mode):
    monkeypatch.setenv('CSV_TEMPLATE', template)
    monkeypatch.setenv('CSV_MODE', mode)
    project = scaffold(tmp_path / 'orders project', template, FIXTURES / 'spec.md')
    config = load_config(project)
    config['max_develop_seconds'] = 120
    config['max_revisions'] = 1 if mode == 'revise' else 0
    unit = config['checks'][0]
    unit['timeout_seconds'] = 15
    if template == 'python-cli':
        regression_argv = [sys.executable, '-m', 'pytest', '-q', 'tests/test_cli.py',
                           '--junitxml=.agentcrew-results/regression.xml']
    else:
        regression_argv = ['node', '--experimental-strip-types', '--test', '--test-reporter=junit',
                           '--test-reporter-destination=.agentcrew-results/regression.xml', 'tests/cli.test.ts']
    config['checks'].append({'name': 'regression', 'argv': regression_argv, 'timeout_seconds': 15,
                            'result_format': 'junit', 'result_file': '.agentcrew-results/regression.xml'})
    source = git(project, 'rev-parse', 'HEAD')
    originals = {path.relative_to(project).as_posix(): path.read_bytes() for path in project.rglob('*')
                 if path.is_file() and '.git' not in path.relative_to(project).parts}
    db = Database(tmp_path / 'state')
    run_id = start_develop(db, project, FIXTURES / 'spec.md', config)
    tmux = Tmux(db)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = db.run(run_id)
            if run['state'] in {'completed', 'failed', 'needs_attention'}:
                break
            time.sleep(.1)
        else:
            raise AssertionError('CSV workflow timeout: ' + json.dumps(db.run(run_id)) + '\n' +
                                 json.dumps(db.rows('SELECT * FROM tasks'), indent=2))
    finally:
        for seat in db.rows('SELECT * FROM seats WHERE scope_id=?', (run_id,)):
            tmux.stop(seat['tmux_session'])
        tmux.stop('supervisor')
    assert git(project, 'rev-parse', 'HEAD') == source
    assert git(project, 'status', '--porcelain') == b''
    for relative, original in originals.items():
        assert (project / relative).read_bytes() == original
    return project, db, run_id, db.run(run_id), provider


def hidden_behaviors(project, template, tmp_path):
    """Independent values/CSV parser and filesystem failure; never sent to the writer."""
    argv = ([sys.executable, 'orders_cli.py'] if template == 'python-cli' else
            ['node', '--experimental-strip-types', 'src/cli.ts'])
    source = project / 'data/orders.json'
    original_source = source.read_bytes()
    output = tmp_path / 'hidden output.csv'
    try:
        source.write_text('[]')
        empty = command([*argv, 'export-orders', '--output', str(output)], project)
        assert empty.returncode == 0, empty.stderr
        with output.open(encoding='utf-8', newline='') as stream:
            assert list(csv.reader(stream)) == [['id', 'total', 'currency']]
        orders = [{'id': 'z', 'total': '4.25', 'currency': 'RUB'},
                  {'id': 'a', 'total': '1,234', 'currency': 'EU"R\nрубль'}]
        source.write_text(json.dumps(orders, ensure_ascii=False), encoding='utf-8')
        output.write_bytes(b'precious existing file\x00')
        refused = command([*argv, 'export-orders', '--output', str(output)], project)
        assert refused.returncode == 2 and refused.stderr.strip()
        assert output.read_bytes() == b'precious existing file\x00'
        forced = command([*argv, 'export-orders', '--output', str(output), '--force'], project)
        assert forced.returncode == 0, forced.stderr
        with output.open(encoding='utf-8', newline='') as stream:
            assert list(csv.DictReader(stream)) == [orders[1], orders[0]]
        assert 'рубль' in output.read_text(encoding='utf-8')
        assert command([*argv, 'status'], project).stdout == 'orders: 2\n'
        protected = b'unchanged after failed forced write\x00'
        output.write_bytes(protected)
        if template == 'python-cli':
            script = ('import os; from orders_cli import main; '
                      'os.replace=lambda *args: (_ for _ in ()).throw(OSError("hidden write failure")); '
                      f'raise SystemExit(main(["export-orders","--output",{str(output)!r},"--force"]))')
            failed = command([sys.executable, '-c', script], project)
        else:
            script = ("import fs from 'node:fs'; import { syncBuiltinESMExports } from 'node:module'; "
                      "fs.renameSync=()=>{ throw new Error('hidden write failure'); }; syncBuiltinESMExports(); "
                      "const {main}=await import('./src/cli.ts'); "
                      f"process.exitCode=main(['export-orders','--output',{json.dumps(str(output))},'--force']);")
            failed = command(['node', '--experimental-strip-types', '--input-type=module', '-e', script], project)
        assert failed.returncode != 0 and failed.stderr.strip()
        assert output.read_bytes() == protected
    finally:
        source.write_bytes(original_source)


@pytest.mark.parametrize(('template', 'mode'), [('python-cli', 'revise'), ('typescript-cli', 'normal')])
def test_csv_develop_new_apply_and_hidden_behavior(tmp_path, monkeypatch, provider, template, mode):
    project, db, run_id, run, calls_file = run_csv(tmp_path, monkeypatch, provider, template, mode)
    try:
        assert run['state'] == 'completed', json.dumps(db.rows('SELECT * FROM tasks'), indent=2)
        assert run['outcome'] == 'accepted', json.dumps(db.rows('SELECT * FROM requirement_results'), indent=2)
        candidates = db.rows('SELECT * FROM candidates WHERE run_id=? ORDER BY revision', (run_id,))
        assert len(candidates) == (2 if mode == 'revise' else 1)
        current = candidates[-1]
        requirements = db.rows('SELECT * FROM requirement_results WHERE candidate_id=?', (current['id'],))
        assert len(requirements) == 8 and all(row['status'] == 'passed' for row in requirements)
        evidence = [json.loads(row['evidence_json']) for row in requirements]
        assert all(proof['candidate_sha'] == current['commit_sha'] for proof in evidence)
        reviews = db.rows("SELECT * FROM tasks WHERE candidate_id=? AND kind IN ('correctness','tests')", (current['id'],))
        assert len(reviews) == 2 and all(task['state'] == 'done' for task in reviews)
        directories = {json.loads(task['body_json'])['cwd'] for task in reviews}
        assert len(directories) == 2
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
        assert any(call['kind'] == 'plan' for call in calls)
        assert any(call['kind'] == 'implement' for call in calls)
        assert {'correctness', 'tests'} <= {call['kind'] for call in calls}
        if mode == 'revise':
            assert sum(call['kind'] == 'revise' for call in calls) == 1
            prior = db.row("SELECT r.evidence_json FROM requirement_results r JOIN requirements q ON q.id=r.requirement_id "
                           "WHERE r.candidate_id=? AND q.label='A8'", (candidates[0]['id'],))
            assert prior is not None
            old_check = json.loads(prior['evidence_json'])['check_id']
            assert old_check not in {proof['check_id'] for proof in evidence}
            assert db.row('SELECT state FROM check_runs WHERE id=?', (old_check,))['state'] == 'passed'
            assert candidates[1]['parent_candidate_id'] == candidates[0]['id']
        preview = apply_candidate(db, run_id)
        assert preview['candidate_id'] == current['id'] and not preview['applied']
        assert 'export-orders' in preview['patch']
        apply_candidate(db, run_id, dry_run=False)
        hidden_behaviors(project, template, tmp_path)
        with pytest.raises(UserError):
            apply_candidate(db, run_id, dry_run=False)
    finally:
        db.close()


def test_green_checks_missing_exact_criterion_blocks_acceptance(tmp_path, monkeypatch, provider):
    _project, db, _run_id, run, _calls = run_csv(tmp_path, monkeypatch, provider, 'python-cli', 'unknown')
    try:
        assert run['state'] == 'needs_attention' and run['outcome'] == 'partial'
        assert all(check['state'] == 'passed' for check in db.rows('SELECT * FROM check_runs'))
        criterion = db.row("SELECT r.status FROM requirement_results r JOIN requirements q ON q.id=r.requirement_id "
                           "WHERE q.label='A3'")
        assert criterion['status'] == 'unknown'
        with pytest.raises(UserError):
            apply_candidate(db, run['id'], dry_run=False)
    finally:
        db.close()


def test_failed_reviewer_remains_partial_with_saved_candidate(tmp_path, monkeypatch, provider):
    _project, db, _run_id, run, _calls = run_csv(tmp_path, monkeypatch, provider, 'typescript-cli', 'reviewer-failed')
    try:
        assert run['state'] == 'completed' and run['outcome'] == 'partial'
        assert run['current_candidate_id']
        assert db.row("SELECT state FROM tasks WHERE kind='tests'")['state'] == 'failed'
        assert db.row("SELECT state FROM tasks WHERE kind='correctness'")['state'] == 'done'
        with pytest.raises(UserError):
            apply_candidate(db, run['id'], dry_run=False)
    finally:
        db.close()


def test_invalid_reviewer_is_partial_and_cannot_apply(tmp_path, monkeypatch, provider):
    _project, db, _run_id, run, _calls = run_csv(tmp_path, monkeypatch, provider, 'python-cli', 'reviewer-invalid')
    try:
        assert run['outcome'] == 'partial', json.dumps(db.rows('SELECT * FROM tasks'), indent=2)
        assert run['current_candidate_id']
        assert db.row("SELECT state FROM tasks WHERE kind='tests'")['state'] == 'failed'
        assert db.row("SELECT count(*) AS n FROM events WHERE kind='provider_output_invalid'")['n'] >= 1
        with pytest.raises(UserError):
            apply_candidate(db, run['id'], dry_run=False)
    finally:
        db.close()
