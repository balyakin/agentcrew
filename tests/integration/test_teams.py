"""Real tmux/Git/SQLite team lifecycle with explicit controller artifacts."""

import importlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from agentcrew.db import Database
from agentcrew.models import UserError
from agentcrew.queue import Queue
from agentcrew.tmux_backend import Tmux


def git_command(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True).stdout


@pytest.fixture
def teams_module():
    assert importlib.util.find_spec('agentcrew.teams'), 'Team implementation is missing'
    return importlib.import_module('agentcrew.teams')


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    git_command(root, 'init', '-b', 'main')
    git_command(root, 'config', 'user.name', 'Fixture')
    git_command(root, 'config', 'user.email', 'fixture@local')
    (root / 'app.py').write_text('value = 1\n')
    git_command(root, 'add', '.')
    git_command(root, 'commit', '-m', 'baseline')
    return root


@pytest.fixture
def team(tmp_path, project, teams_module):
    db = Database(tmp_path / 'state')
    identifier = teams_module.up(db, project, 'pair')
    tmux = Tmux(db)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            seats = db.rows('SELECT * FROM seats WHERE scope_id=?', (identifier,))
            if all(seat['state'] == 'ready' for seat in seats):
                break
            time.sleep(.05)
        assert all(seat['state'] == 'ready' for seat in seats)
        # Controller behavior is exercised manually; no paid provider is invoked.
        teams_module.stop(db, identifier)
        yield db, identifier
    finally:
        for seat in db.rows('SELECT * FROM seats WHERE scope_id=?', (identifier,)):
            tmux.stop(seat['tmux_session'])
        tmux.stop('supervisor')
        db.close()


def complete_code_task(db, identifier, teams_module):
    owner = f'dev-owner@{identifier}'
    task_id = teams_module.create_task(db, owner, 'Change value to 2.')
    task = db.row('SELECT * FROM tasks WHERE id=?', (task_id,))
    body = json.loads(task['body_json'])
    path = Path(body['cwd'])
    (path / 'app.py').write_text('value = 2\n')
    with db.transaction():
        db.set_state('scopes', identifier, 'running')
    claimed = Queue(db).claim(owner, 'manual-controller')
    assert claimed['id'] == task_id
    seat = db.row('SELECT * FROM seats WHERE id=?', (owner,))
    result = {'data': {'schema_version': 1, 'summary': 'Changed value', 'changed_files': ['app.py'],
                       'addressed_requirements': [], 'remaining_requirements': [], 'limitations': []}}
    result['artifact'] = teams_module.freeze_task(db, seat, claimed, result)
    assert Queue(db).finish(task_id, 'manual-controller', claimed['attempt'], result)
    db.set_state('scopes', identifier, 'paused')
    return task_id, result['artifact']


def test_pair_artifact_handoff_and_durable_message(team, project, teams_module):
    db, identifier = team
    source_sha = git_command(project, 'rev-parse', 'HEAD')
    task, artifact = complete_code_task(db, identifier, teams_module)
    reviewer = f'dev-check@{identifier}'
    queue = Queue(db)
    message = queue.send(identifier, 'user', reviewer, 'Check value behavior', 'once')
    assert queue.send(identifier, 'user', reviewer, 'Check value behavior', 'once') == message
    child = teams_module.handoff(db, task, reviewer, 'Review this exact code result.')
    child_row = db.row('SELECT * FROM tasks WHERE id=?', (child,))
    body = json.loads(child_row['body_json'])
    review_path = Path(body['cwd'])
    assert child_row['parent_task_id'] == task
    assert body['access'] == 'read-only'
    assert body['candidate_sha'] == artifact['commit_sha']
    assert (review_path / 'app.py').read_text() == 'value = 2\n'
    assert git_command(review_path, 'rev-parse', 'HEAD').decode().strip() == artifact['commit_sha']
    assert queue.inbox(reviewer)[0]['id'] == message
    assert (project / 'app.py').read_text() == 'value = 1\n'
    assert git_command(project, 'rev-parse', 'HEAD') == source_sha
    assert git_command(project, 'status', '--porcelain') == b''
    with pytest.raises(UserError):
        teams_module.handoff(db, task, reviewer, 'Duplicate must not create another child.')
    assert db.row('SELECT count(*) AS n FROM tasks WHERE parent_task_id=?', (task,))['n'] == 1


def test_handoff_refuses_uncommitted_sender_changes(team, teams_module):
    db, identifier = team
    task, artifact = complete_code_task(db, identifier, teams_module)
    (Path(artifact['worktree_path']) / 'app.py').write_text('partial user edit\n')
    with pytest.raises(UserError):
        teams_module.handoff(db, task, f'dev-check@{identifier}', 'Unsafe')
    assert db.row('SELECT count(*) AS n FROM tasks WHERE parent_task_id=?', (task,))['n'] == 0


def test_freeze_checks_reported_files_and_source_boundaries(team, teams_module, project):
    db, identifier = team
    owner = f'dev-owner@{identifier}'
    task_id = teams_module.create_task(db, owner, 'Change the app')
    task = db.row('SELECT * FROM tasks WHERE id=?', (task_id,))
    path = Path(json.loads(task['body_json'])['cwd'])
    (path / 'app.py').write_text('value = 3\n')
    result = {'data': {'schema_version': 1, 'summary': 'Wrong file list', 'changed_files': [],
                       'addressed_requirements': [], 'remaining_requirements': [], 'limitations': []}}
    seat = db.row('SELECT * FROM seats WHERE id=?', (owner,))
    Tmux(db).stop('supervisor')
    db.set_state('scopes', identifier, 'running')
    task = Queue(db).claim(owner, 'manual-controller')
    with pytest.raises(UserError) as error:
        teams_module.freeze_task(db, seat, task, result)
    assert error.value.code == 'provider_output_invalid'
    db.set_state('scopes', identifier, 'paused')
    assert (project / 'app.py').read_text() == 'value = 1\n'
    assert git_command(path, 'log', '--oneline').count(b'\n') == 1


def test_snapshot_preserves_partial_writer_and_resume_requires_inspection(team, teams_module):
    db, identifier = team
    owner = f'dev-owner@{identifier}'
    task_id = teams_module.create_task(db, owner, 'Write a result')
    task = db.row('SELECT * FROM tasks WHERE id=?', (task_id,))
    path = Path(json.loads(task['body_json'])['cwd'])
    (path / 'app.py').write_text('unfinished writer content\n')
    db.set_state('scopes', identifier, 'running')
    claimed = Queue(db).claim(owner, 'lost-worker')
    assert claimed['id'] == task_id
    stopped = teams_module.stop(db, identifier, snapshot=True)
    assert db.row('SELECT state,attempt FROM tasks WHERE id=?', (task_id,)) == {'state': 'blocked', 'attempt': 1}
    assert Path(stopped['snapshot']).is_file()
    assert Path(stopped['manifest']).is_file()
    manifest = json.loads(Path(stopped['manifest']).read_text())
    assert manifest['team_id'] == identifier
    assert any(seat['id'] == owner for seat in manifest['seats'])
    backup = sqlite3.connect(stopped['snapshot'])
    try:
        assert backup.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()[0] == 'blocked'
    finally:
        backup.close()
    resumed = teams_module.resume(db, identifier)
    assert not resumed['resumed']
    assert db.scope(identifier)['state'] == 'needs_attention'
    assert (path / 'app.py').read_text() == 'unfinished writer content\n'
    assert db.row('SELECT count(*) AS n FROM tasks')['n'] == 1


def test_templates_and_invalid_input_precede_state_mutations(project, tmp_path, teams_module):
    db = Database(tmp_path / 'state')
    for template in ('review', 'adversarial-review'):
        identifier = teams_module.up(db, project, template, provider='claude')
        seats = db.rows('SELECT * FROM seats WHERE scope_id=?', (identifier,))
        assert len(seats) == (2 if template == 'review' else 3)
        assert all(seat['provider'] == 'claude' for seat in seats)
        teams_module.stop(db, identifier)
    before = db.row('SELECT count(*) AS n FROM scopes')['n']
    with pytest.raises(UserError):
        teams_module.up(db, project, 'unknown-template')
    assert db.row('SELECT count(*) AS n FROM scopes')['n'] == before
    (project / 'dirty').write_text('save me')
    with pytest.raises(UserError):
        teams_module.up(db, project, 'pair')
    assert (project / 'dirty').read_text() == 'save me'
    Tmux(db).stop('supervisor')
    db.close()


def test_handoff_chain_preserves_reader_artifact_and_bounds_steps(team, teams_module):
    db, identifier = team
    stored = json.loads(db.row('SELECT config_json FROM teams WHERE id=?', (identifier,))['config_json'])
    stored['max_steps'] = 1
    db.execute('UPDATE teams SET config_json=? WHERE id=?', (json.dumps(stored), identifier))
    parent, artifact = complete_code_task(db, identifier, teams_module)
    child = teams_module.handoff(db, parent, f'dev-check@{identifier}', 'Review the committed result.')
    Queue(db).complete(child, {'artifact': artifact, 'summary': 'Read-only check complete'})
    with pytest.raises(UserError):
        teams_module.handoff(db, child, f'dev-owner@{identifier}', 'This would exceed the step limit.')
    assert db.row('SELECT count(*) AS n FROM tasks')['n'] == 2
    assert db.row('SELECT state FROM tasks WHERE id=?', (child,))['state'] == 'done'
    assert db.scope(identifier)['state'] == 'needs_attention'


def test_queued_writers_have_independent_clean_baselines(team, teams_module):
    db, identifier = team
    owner = f'dev-owner@{identifier}'
    one = teams_module.create_task(db, owner, 'First independent task')
    two = teams_module.create_task(db, owner, 'Second independent task')
    body1 = json.loads(db.row('SELECT body_json FROM tasks WHERE id=?', (one,))['body_json'])
    body2 = json.loads(db.row('SELECT body_json FROM tasks WHERE id=?', (two,))['body_json'])
    assert body1['cwd'] != body2['cwd']
    assert body1['baseline_sha'] == body2['baseline_sha']
    (Path(body1['cwd']) / 'app.py').write_text('partial first task\n')
    assert (Path(body2['cwd']) / 'app.py').read_text() == 'value = 1\n'


def test_automatic_handoff_is_once_and_pause_blocks_dispatch(team, teams_module):
    db, identifier = team
    task, _artifact = complete_code_task(db, identifier, teams_module)
    stored = json.loads(db.row('SELECT config_json FROM teams WHERE id=?', (identifier,))['config_json'])
    stored['links'][0]['automatic'] = True
    db.execute('UPDATE teams SET config_json=? WHERE id=?', (json.dumps(stored), identifier))
    assert teams_module.advance(db, identifier) == []
    Tmux(db).stop('supervisor')
    db.set_state('scopes', identifier, 'running')
    children = teams_module.advance(db, identifier)
    db.set_state('scopes', identifier, 'paused')
    assert len(children) == 1
    assert db.row('SELECT parent_task_id FROM tasks WHERE id=?', (children[0],))['parent_task_id'] == task
    assert teams_module.advance(db, identifier) == []
    assert db.row('SELECT count(*) AS n FROM tasks WHERE parent_task_id=?', (task,))['n'] == 1
