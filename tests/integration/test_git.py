"""Real Git behavior: source preservation, immutable snapshots, and safe apply."""

import importlib
import subprocess
from pathlib import Path

import pytest

from agentcrew.models import UserError


def command(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
    return result.stdout


@pytest.fixture
def snapshots():
    # Missing snapshot implementation is an explicit RED failure, not a collection error.
    spec = importlib.util.find_spec('agentcrew.git_snapshot')
    assert spec is not None, 'Git snapshot implementation is missing'
    return importlib.import_module('agentcrew.git_snapshot')


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'source repo'
    root.mkdir()
    command(root, 'init', '-b', 'main')
    command(root, 'config', 'user.name', 'Fixture')
    command(root, 'config', 'user.email', 'fixture@local')
    (root / 'code with spaces.py').write_text('alpha\nbeta\ngamma\n')
    (root / 'binary.dat').write_bytes(b'\x00original\xff')
    command(root, 'add', '.')
    command(root, 'commit', '-m', 'baseline')
    return root


def start_run(repo, tmp_path, snapshots, workflow='develop', base=None):
    from agentcrew.db import Database
    snapshot = snapshots.preflight(repo, workflow, base)
    db = Database(tmp_path / 'state')
    run = db.create_scope('run', str(repo))
    db.execute('INSERT INTO runs(id,workflow,source_head_sha,base_sha,phase,config_json) '
               'VALUES(?,?,?,?,?,?)',
               (run, workflow, snapshot['source_head_sha'], snapshot['base_sha'], 'implement', '{}'))
    return db, run, snapshot


def writer(db, run, snapshot, snapshots):
    branch = f'agentcrew/develop/{run[:8]}/r1'
    path = snapshots.create_worktree(db, run, snapshot['source_head_sha'], 'implementer', branch=branch)
    return path, branch


def test_preflight_empty_develop_and_nonempty_review(repo, snapshots):
    snapshot = snapshots.preflight(repo, 'develop')
    assert snapshot['base_sha'] == snapshot['source_head_sha']
    assert snapshot['diff'] == b''
    assert snapshot['changed_files'] == []
    with pytest.raises(UserError):
        snapshots.preflight(repo, 'review')
    with pytest.raises(UserError):
        snapshots.preflight(repo, 'review', 'main')
    command(repo, 'switch', '-c', 'feature')
    (repo / 'code with spaces.py').write_text('alpha\nchanged\ngamma\n')
    command(repo, 'commit', '-am', 'feature')
    snapshot = snapshots.preflight(repo, 'review', 'main')
    assert snapshot['changed_files'] == ['code with spaces.py']
    assert b'+changed' in snapshot['diff']
    assert snapshots.changed_lines(repo, snapshot['base_sha'], snapshot['source_head_sha']) == {
        'code with spaces.py': {2}}


@pytest.mark.parametrize('state', ['untracked', 'staged', 'modified'])
def test_preflight_refuses_all_dirty_sources(repo, snapshots, state):
    if state == 'untracked':
        (repo / 'untracked.txt').write_text('save me')
    else:
        (repo / 'code with spaces.py').write_text('unsaved')
        if state == 'staged':
            command(repo, 'add', '.')
    with pytest.raises(UserError):
        snapshots.preflight(repo, 'develop')
    assert 'unsaved' in (repo / 'code with spaces.py').read_text() or state == 'untracked'


def test_preflight_lists_symlinks_without_following_them(repo, snapshots, tmp_path):
    command(repo, 'switch', '-c', 'feature')
    outside = tmp_path / 'secret'
    outside.write_text('private')
    (repo / 'link').symlink_to(outside)
    command(repo, 'add', '.')
    command(repo, 'commit', '-m', 'link')
    snapshot = snapshots.preflight(repo, 'review', 'main')
    assert any(item['path'] == 'link' and item['kind'] == 'symlink' for item in snapshot['special_files'])
    assert b'private' not in snapshot['diff']


def test_worktrees_freeze_binary_spaces_and_source_preservation(repo, snapshots, tmp_path):
    db, run, snapshot = start_run(repo, tmp_path, snapshots)
    source_sha = snapshot['source_head_sha']
    path, branch = writer(db, run, snapshot, snapshots)
    hooks = Path(command(repo, 'rev-parse', '--git-common-dir').decode().strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hook = hooks / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 99\n')
    hook.chmod(0o755)
    (path / 'code with spaces.py').write_text('alpha\nchanged\ngamma\n')
    (path / 'binary.dat').write_bytes(b'\x00candidate\xfe')
    candidate = snapshots.freeze(path, db, run, branch=branch)
    row = db.row('SELECT * FROM candidates WHERE id=?', (candidate,))
    assert row['commit_sha'] != source_sha
    assert row['branch_name'] == branch
    assert command(repo, 'rev-parse', 'HEAD').decode().strip() == source_sha
    assert (repo / 'binary.dat').read_bytes() == b'\x00original\xff'
    assert (repo / 'code with spaces.py').read_text() == 'alpha\nbeta\ngamma\n'
    assert command(repo, 'status', '--porcelain') == b''
    assert snapshots.assert_snapshot(path, row['commit_sha'])
    review = snapshots.create_worktree(db, run, row['commit_sha'], 'reviewer', candidate_id=candidate)
    detached = subprocess.run(['git', '-C', str(review), 'symbolic-ref', '-q', 'HEAD'], capture_output=True, check=False)
    assert detached.returncode == 1
    assert snapshots.assert_snapshot(review, row['commit_sha'])
    (review / 'binary.dat').write_bytes(b'changed')
    assert not snapshots.assert_snapshot(review, row['commit_sha'])
    states = db.rows('SELECT state FROM worktrees WHERE scope_id=?', (run,))
    assert states == [{'state': 'ready'}, {'state': 'ready'}]


def test_failed_worktree_add_is_saved(repo, snapshots, tmp_path):
    db, run, _snapshot = start_run(repo, tmp_path, snapshots)
    with pytest.raises(UserError):
        snapshots.create_worktree(db, run, 'f' * 40, 'reviewer')
    assert db.row('SELECT state FROM worktrees WHERE scope_id=?', (run,)) == {'state': 'failed'}
    assert db.row("SELECT count(*) AS n FROM events WHERE kind='worktree.creating'")['n'] == 1


def test_freeze_refuses_symlink_escape_and_oversized_files(repo, snapshots, tmp_path):
    db, run, snapshot = start_run(repo, tmp_path, snapshots)
    path, branch = writer(db, run, snapshot, snapshots)
    outside = tmp_path / 'private'
    outside.write_text('secret')
    (path / 'escape').symlink_to(outside)
    with pytest.raises(UserError):
        snapshots.freeze(path, db, run, branch=branch)
    assert db.row('SELECT count(*) AS n FROM candidates')['n'] == 0
    (path / 'escape').unlink()
    with (path / 'large').open('wb') as stream:
        stream.truncate(21 * 1024 * 1024)
    with pytest.raises(UserError):
        snapshots.freeze(path, db, run, branch=branch)
    assert command(repo, 'status', '--porcelain') == b''


def test_apply_uses_source_not_review_base_and_preserves_binary(repo, snapshots, tmp_path):
    command(repo, 'switch', '-c', 'feature')
    (repo / 'already present.txt').write_text('existing feature\n')
    command(repo, 'add', '.')
    command(repo, 'commit', '-m', 'existing feature')
    db, run, snapshot = start_run(repo, tmp_path, snapshots, 'review', 'main')
    path, branch = writer(db, run, snapshot, snapshots)
    (path / 'code with spaces.py').write_text('alpha\nfixed\ngamma\n')
    (path / 'binary.dat').write_bytes(b'\x00fixed\xff')
    snapshots.freeze(path, db, run, branch=branch)
    db.execute("UPDATE runs SET outcome='accepted' WHERE id=?", (run,))
    preview = snapshots.apply_candidate(db, run)
    assert 'already present.txt' not in preview['patch']
    assert (repo / 'code with spaces.py').read_text() == 'alpha\nbeta\ngamma\n'
    applied = snapshots.apply_candidate(db, run, dry_run=False)
    assert applied['applied']
    assert (repo / 'already present.txt').read_text() == 'existing feature\n'
    assert (repo / 'code with spaces.py').read_text() == 'alpha\nfixed\ngamma\n'
    assert (repo / 'binary.dat').read_bytes() == b'\x00fixed\xff'
    assert command(repo, 'rev-parse', 'HEAD').decode().strip() == snapshot['source_head_sha']
    with pytest.raises(UserError):
        snapshots.apply_candidate(db, run, dry_run=False)


@pytest.mark.parametrize('change', ['unaccepted', 'head', 'dirty'])
def test_apply_refuses_unaccepted_stale_or_dirty_sources(repo, snapshots, tmp_path, change):
    db, run, snapshot = start_run(repo, tmp_path, snapshots)
    path, branch = writer(db, run, snapshot, snapshots)
    (path / 'code with spaces.py').write_text('result\n')
    snapshots.freeze(path, db, run, branch=branch)
    if change != 'unaccepted':
        db.execute("UPDATE runs SET outcome='accepted' WHERE id=?", (run,))
    if change == 'head':
        command(repo, 'commit', '--allow-empty', '-m', 'new source head')
    elif change == 'dirty':
        (repo / 'keep me').write_text('unsaved')
    before = command(repo, 'status', '--porcelain')
    with pytest.raises(UserError):
        snapshots.apply_candidate(db, run, dry_run=False)
    assert command(repo, 'status', '--porcelain') == before
    assert (repo / 'code with spaces.py').read_text() == 'alpha\nbeta\ngamma\n'


def test_cleanup_requires_ownership_clean_tree_and_explicit_write(repo, snapshots, tmp_path):
    db, run, snapshot = start_run(repo, tmp_path, snapshots)
    path = snapshots.create_worktree(db, run, snapshot['source_head_sha'], 'reviewer')
    row = db.row('SELECT id FROM worktrees WHERE path=?', (str(path),))
    plan = snapshots.cleanup_worktree(db, row['id'])
    assert plan['dry_run'] and path.exists()
    (path / 'precious.txt').write_text('user content')
    with pytest.raises(UserError):
        snapshots.cleanup_worktree(db, row['id'], dry_run=False)
    assert (path / 'precious.txt').read_text() == 'user content'
    (path / 'precious.txt').unlink()
    snapshots.cleanup_worktree(db, row['id'], dry_run=False)
    assert not path.exists()
    assert db.row('SELECT state FROM worktrees WHERE id=?', (row['id'],))['state'] == 'removed'
    db.execute('INSERT INTO worktrees(id,scope_id,path,state,created_at) VALUES(?,?,?,?,?)',
               ('foreign', run, str(repo), 'ready', 'now'))
    with pytest.raises(UserError):
        snapshots.cleanup_worktree(db, 'foreign', dry_run=False)
    assert (repo / 'code with spaces.py').exists()


def test_freeze_checks_agent_committed_symlink_escape(repo, snapshots, tmp_path):
    db, run, snapshot = start_run(repo, tmp_path, snapshots)
    path, branch = writer(db, run, snapshot, snapshots)
    outside = tmp_path / 'secret'
    outside.write_text('private')
    (path / 'escape').symlink_to(outside)
    command(path, 'add', '.')
    command(path, 'commit', '-m', 'agent committed unsafe path')
    with pytest.raises(UserError):
        snapshots.freeze(path, db, run, branch=branch)
    assert db.row('SELECT count(*) AS n FROM candidates')['n'] == 0
    assert (repo / 'code with spaces.py').read_text() == 'alpha\nbeta\ngamma\n'


def test_freeze_refuses_modified_git_pointer(repo, snapshots, tmp_path):
    db, run, snapshot = start_run(repo, tmp_path, snapshots)
    path, branch = writer(db, run, snapshot, snapshots)
    (path / '.git').write_text(f'gitdir: {repo / ".git"}\n')
    (path / 'code with spaces.py').write_text('unsafe result')
    with pytest.raises(UserError):
        snapshots.freeze(path, db, run, branch=branch)
    assert db.row('SELECT count(*) AS n FROM candidates')['n'] == 0
    assert (repo / 'code with spaces.py').read_text() == 'alpha\nbeta\ngamma\n'


def test_apply_rechecks_source_immediately_after_git_check(repo, snapshots, tmp_path, monkeypatch):
    db, run, snapshot = start_run(repo, tmp_path, snapshots)
    path, branch = writer(db, run, snapshot, snapshots)
    (path / 'code with spaces.py').write_text('candidate result\n')
    snapshots.freeze(path, db, run, branch=branch)
    db.execute("UPDATE runs SET outcome='accepted' WHERE id=?", (run,))
    real_git = snapshots.git

    def source_changed_during_check(directory, *args, **kwargs):
        result = real_git(directory, *args, **kwargs)
        if args[:2] == ('apply', '--check'):
            (repo / 'code with spaces.py').write_text('late user edit\n')
        return result

    monkeypatch.setattr(snapshots, 'git', source_changed_during_check)
    with pytest.raises(UserError):
        snapshots.apply_candidate(db, run, dry_run=False)
    assert (repo / 'code with spaces.py').read_text() == 'late user edit\n'
    assert db.row("SELECT count(*) AS n FROM events WHERE kind='apply.completed'")['n'] == 0


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_preflight_refuses_hidden_index_changes(repo, snapshots, flag):
    command(repo, 'update-index', flag, 'code with spaces.py')
    (repo / 'code with spaces.py').write_text('hidden user edit\n')
    assert command(repo, 'status', '--porcelain') == b''
    with pytest.raises(UserError):
        snapshots.preflight(repo, 'develop')
    assert (repo / 'code with spaces.py').read_text() == 'hidden user edit\n'
