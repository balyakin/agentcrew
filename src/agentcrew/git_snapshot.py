"""Bounded Git snapshots, isolated worktrees, and explicit source patch application."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .models import MAX_LOG, UserError, digest, now, redact, uid

MAX_FILES = 1000
GIT_TIMEOUT = 30


def git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    """Run Git without a shell, user hooks, unbounded logs, or global config writes."""
    if input is not None and len(input) > MAX_LOG:
        raise UserError('Git input exceeds 20 MiB limit', 'git_error')
    try:
        with tempfile.TemporaryDirectory(prefix='agentcrew-hooks-') as hooks, \
                tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.run(
                ['git', '-c', f'core.hooksPath={hooks}', '-C', str(repo), *args],
                input=input, stdout=stdout, stderr=stderr, timeout=GIT_TIMEOUT, check=False,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
            )
            if stdout.tell() > MAX_LOG or stderr.tell() > MAX_LOG:
                raise UserError('Git output exceeds 20 MiB limit', 'git_error')
            stderr.seek(0)
            if process.returncode:
                detail = redact(stderr.read(4096).decode('utf-8', errors='replace')).strip()
                raise UserError(f'Git failed: {detail}', 'git_error')
            stdout.seek(0)
            return stdout.read(MAX_LOG + 1)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError(f'Git unavailable or timed out: {exc}', 'git_error') from exc


def _sha(repo: Path, ref: str) -> str:
    if not ref or '\x00' in ref:
        raise UserError('A Git commit reference is required', 'git_error')
    return git(repo, 'rev-parse', '--verify', '--end-of-options', f'{ref}^{{commit}}').decode().strip()


def _clean(repo: Path, *, ignored: bool = False) -> bool:
    args = ['status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none']
    if ignored:
        args.append('--ignored')
    if git(repo, *args):
        return False
    # Git status hides edits to assume-unchanged and skip-worktree entries.
    entries = git(repo, 'ls-files', '-v', '-z').split(b'\x00')
    return not any(entry and (entry[:1] == b'S' or entry[:1].islower()) for entry in entries)


def diff(repo: Path, base: str, head: str) -> bytes:
    return git(repo, 'diff', '--binary', '--no-ext-diff', '--no-textconv', '--no-renames',
               _sha(repo, base), _sha(repo, head), '--')


def _files(repo: Path, base: str, head: str) -> list[str]:
    raw = git(repo, 'diff', '--name-only', '-z', '--no-renames', base, head, '--')
    files = [os.fsdecode(name) for name in raw.split(b'\x00') if name]
    if len(files) > MAX_FILES:
        raise UserError('Candidate exceeds 1000 changed files', 'git_error')
    return files


def preflight(repo: Path, workflow: str, base: str | None = None) -> dict:
    if workflow not in {'review', 'develop', 'fix'}:
        raise UserError('Unsupported Git workflow')
    root = Path(os.fsdecode(git(Path(repo), 'rev-parse', '--show-toplevel').rstrip(b'\n'))).resolve()
    if not _clean(root):
        raise UserError('Commit or save all staged, modified, and untracked files before starting', 'dirty_source')
    head = _sha(root, 'HEAD')
    if workflow == 'review':
        if not base:
            raise UserError('Review requires --base')
        base_sha = git(root, 'merge-base', _sha(root, base), head).decode().strip()
    else:
        base_sha = head if workflow == 'develop' or base is None else _sha(root, base)
    patch = diff(root, base_sha, head)
    if workflow == 'review' and not patch:
        raise UserError('Review requires a nonempty diff from merge-base', 'empty_diff')
    files = _files(root, base_sha, head)
    special = []
    for revision in dict.fromkeys((base_sha, head)):
        for entry in git(root, 'ls-tree', '-rz', revision, '--').split(b'\x00'):
            if not entry:
                continue
            metadata, name = entry.split(b'\t', 1)
            mode = metadata.split(b' ')[0]
            path = os.fsdecode(name)
            if path in files and mode in {b'120000', b'160000'}:
                item = {'path': path, 'kind': 'symlink' if mode == b'120000' else 'submodule'}
                if item not in special:
                    special.append(item)
    return {'repo_root': str(root), 'source_head_sha': head, 'base_sha': base_sha,
            'diff': patch, 'diff_sha256': digest(patch), 'changed_files': files, 'special_files': special}


def _inside_state(db, path: Path) -> Path:
    root = db.root.resolve(strict=True)
    canonical = path.resolve()
    if canonical == root or not canonical.is_relative_to(root) or path.is_symlink():
        raise UserError('Worktree path is outside the owned state directory', 'worktree_ownership')
    return canonical


def _worktree_state(db, identifier: str, scope: str, state: str, **payload):
    with db.transaction():
        db.execute('UPDATE worktrees SET state=? WHERE id=?', (state, identifier))
        db.event(scope, f'worktree.{state}', {'worktree_id': identifier, **payload})


def create_worktree(db, scope_id: str, sha: str, role: str, *, candidate_id: str | None = None,
                    seat_id: str | None = None, branch: str | None = None) -> Path:
    scope = db.row('SELECT * FROM scopes WHERE id=?', (scope_id,))
    if not scope or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', role):
        raise UserError('Unknown scope or invalid worktree role')
    if candidate_id:
        candidate = db.row('SELECT * FROM candidates WHERE id=?', (candidate_id,))
        if not candidate or candidate['run_id'] != scope_id or candidate['commit_sha'] != sha:
            raise UserError('Worktree candidate belongs to another run or SHA')
    if seat_id:
        seat = db.row('SELECT scope_id FROM seats WHERE id=?', (seat_id,))
        if not seat or seat['scope_id'] != scope_id:
            raise UserError('Worktree seat belongs to another scope')
    if branch and not branch.startswith('agentcrew/'):
        raise UserError('Writer branches must be under agentcrew/')
    identifier = uid()
    path = _inside_state(db, db.root / 'scopes' / scope_id / 'worktrees' / f'{role}-{identifier}')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _inside_state(db, path)
    with db.transaction():
        db.execute('INSERT INTO worktrees VALUES(?,?,?,?,?,?,?,?)',
                   (identifier, scope_id, candidate_id, seat_id, str(path), branch, 'creating', now()))
        db.event(scope_id, 'worktree.creating', {'worktree_id': identifier, 'path': str(path), 'commit_sha': sha})
    try:
        args = ['worktree', 'add']
        args += ['-b', branch] if branch else ['--detach']
        git(Path(scope['repo_root']), *args, str(path), sha)
        _worktree_state(db, identifier, scope_id, 'ready')
    except UserError:
        _worktree_state(db, identifier, scope_id, 'failed')
        raise
    return path


def _owned(db, row: dict) -> tuple[Path, Path]:
    path = _inside_state(db, Path(row['path']))
    if not path.is_dir():
        raise UserError('Recorded worktree is missing; inspect recovery plan', 'worktree_ownership')
    scope = db.row('SELECT repo_root FROM scopes WHERE id=?', (row['scope_id'],))
    if not scope:
        raise UserError('Unknown worktree scope', 'worktree_ownership')
    source = Path(scope['repo_root']).resolve(strict=True)
    paths = []
    for field in git(source, 'worktree', 'list', '--porcelain', '-z').split(b'\x00'):
        if field.startswith(b'worktree '):
            paths.append(Path(os.fsdecode(field[9:])).resolve())
    if path not in paths or path == source:
        raise UserError('Git does not recognize this owned worktree', 'worktree_ownership')
    pointer = path / '.git'
    if pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 4096:
        raise UserError('Worktree Git pointer changed', 'worktree_ownership')
    git_dir = Path(os.fsdecode(git(path, 'rev-parse', '--absolute-git-dir').rstrip(b'\n'))).resolve()
    common = Path(os.fsdecode(git(source, 'rev-parse', '--git-common-dir').rstrip(b'\n')))
    common = (source / common).resolve() if not common.is_absolute() else common.resolve()
    if not git_dir.is_relative_to(common / 'worktrees'):
        raise UserError('Worktree Git pointer belongs to another repository', 'worktree_ownership')
    backpointer = git_dir / 'gitdir'
    try:
        if backpointer.stat().st_size > 4096 or Path(backpointer.read_text().strip()).resolve() != pointer.resolve():
            raise UserError('Worktree Git pointer ownership mismatch', 'worktree_ownership')
    except OSError as exc:
        raise UserError('Worktree Git pointer ownership cannot be verified', 'worktree_ownership') from exc
    return path, source


def _validate_changes(repo: Path, baseline: str):
    changes = git(repo, 'diff', '--name-only', '-z', '--no-renames', baseline, 'HEAD', '--').split(b'\x00')
    changes += git(repo, 'diff', '--name-only', '-z', 'HEAD', '--').split(b'\x00')
    changes += git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\x00')
    names = {os.fsdecode(name) for name in changes if name}
    if len(names) > MAX_FILES:
        raise UserError('Candidate exceeds 1000 changed files', 'candidate_invalid')
    root = repo.resolve(strict=True)
    tracked = {os.fsdecode(name) for name in git(repo, 'ls-tree', '-rz', '--name-only', baseline).split(b'\x00')}
    for name in names:
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '.git' in relative.parts or '\\' in name:
            raise UserError('Invalid changed file path', 'candidate_invalid')
        if name in tracked and (relative.name in {'AGENTS.md', 'CLAUDE.md'} or '.agentcrew' in relative.parts):
            raise UserError('Existing agent instructions or AgentCrew config changed', 'candidate_invalid')
        lexical = root.joinpath(*relative.parts)
        target = lexical.resolve()
        if not target.is_relative_to(root):
            raise UserError('Changed file or symlink escapes the worktree', 'candidate_invalid')
        if lexical.exists() and not lexical.is_file():
            raise UserError('Changed paths must be files; submodule edits require manual inspection', 'candidate_invalid')
        if lexical.exists() and lexical.stat().st_size > MAX_LOG:
            raise UserError('Changed file exceeds 20 MiB limit', 'candidate_invalid')
    return names


def validate_writer_retry(db, task: dict):
    """An explicit retry may retain partial files, never a changed HEAD or ownership."""
    body = json.loads(task['body_json'])
    path = Path(body.get('cwd', ''))
    baseline = body.get('candidate_sha') or body.get('baseline_sha')
    row = db.row('SELECT * FROM worktrees WHERE scope_id=? AND path=? AND state=?',
                 (task['scope_id'], str(path), 'ready'))
    if not baseline or not row or row['seat_id'] != task['owner_seat']:
        raise UserError('Writer retry requires its recorded worktree and baseline', 'isolation_violation')
    path, _ = _owned(db, row)
    if _sha(path, 'HEAD') != baseline:
        raise UserError('Writer HEAD changed; inspect it before creating a new task', 'isolation_violation')
    branch = row['branch_name']
    if (not branch or not branch.startswith('agentcrew/')
            or git(path, 'symbolic-ref', '--quiet', '--short', 'HEAD').decode().strip() != branch):
        raise UserError('Writer retry branch ownership changed', 'isolation_violation')
    _validate_changes(path, baseline)


def freeze(repo: Path, db, run_id: str, *, branch: str | None = None, parent_id=None) -> str:
    path = Path(repo).resolve()
    row = db.row('SELECT * FROM worktrees WHERE scope_id=? AND path=?', (run_id, str(path)))
    run = db.row('SELECT * FROM runs WHERE id=?', (run_id,))
    if not row or not run or row['state'] != 'ready':
        raise UserError('Freeze requires a recorded ready writer worktree', 'worktree_ownership')
    path, _ = _owned(db, row)
    expected = branch or row['branch_name']
    if not expected or not expected.startswith('agentcrew/') or row['branch_name'] != expected:
        raise UserError('Freeze requires the owned AgentCrew writer branch', 'worktree_ownership')
    actual = git(path, 'symbolic-ref', '--quiet', '--short', 'HEAD').decode().strip()
    if actual != expected:
        raise UserError('Writer branch changed', 'worktree_ownership')
    baseline = run['base_sha'] if run['workflow'] == 'fix' else run['source_head_sha']
    if row['candidate_id']:
        previous = db.row('SELECT commit_sha FROM candidates WHERE id=?', (row['candidate_id'],))
        if not previous:
            raise UserError('Recorded writer candidate is missing', 'candidate_invalid')
        baseline = previous['commit_sha']
    if parent_id:
        parent = db.row('SELECT run_id FROM candidates WHERE id=?', (parent_id,))
        if not parent or parent['run_id'] != run_id:
            raise UserError('Candidate parent belongs to another run', 'candidate_invalid')
    _validate_changes(path, baseline)
    hooks = _inside_state(db, db.root / 'empty-hooks')
    hooks.mkdir(mode=0o700, exist_ok=True)
    if any(hooks.iterdir()):
        raise UserError('The disabled hooks directory is no longer empty', 'worktree_ownership')
    git(path, 'add', '--all', '--', '.')
    if git(path, 'diff', '--cached', '--name-only', '-z'):
        git(path, '-c', 'user.name=AgentCrew', '-c', 'user.email=agentcrew@local',
            '-c', 'commit.gpgsign=false', '-c', f'core.hooksPath={hooks}', 'commit', '-m', 'AgentCrew candidate')
    sha = _sha(path, 'HEAD')
    patch = diff(path, run['base_sha'], sha)
    if not _clean(path):
        raise UserError('Writer snapshot changed while freezing', 'candidate_invalid')
    with db.transaction():
        candidate = db.add_candidate(run_id, sha, digest(patch), expected, parent_id=parent_id)
        db.execute('UPDATE worktrees SET candidate_id=? WHERE id=?', (candidate, row['id']))
        db.execute("UPDATE runs SET phase='verify' WHERE id=?", (run_id,))
        db.event(run_id, 'worktree.frozen', {'worktree_id': row['id'], 'candidate_id': candidate})
        db.event(run_id, 'run.phase', {'phase': 'verify', 'candidate_id': candidate})
    return candidate


def assert_snapshot(path: Path, sha: str) -> bool:
    try:
        return _sha(path, 'HEAD') == sha and _clean(path)
    except UserError:
        return False


def changed_lines(repo: Path, base: str, head: str) -> dict[str, set[int]]:
    base, head = _sha(repo, base), _sha(repo, head)
    result = {}
    for path in _files(repo, base, head):
        patch = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--unified=0',
                    base, head, '--', path)
        lines = set()
        for match in re.finditer(rb'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', patch, re.MULTILINE):
            first = int(match[1])
            count = int(match[2]) if match[2] is not None else 1
            lines.update(range(first, first + count))
        result[path] = lines
    return result


def _source_unchanged(repo: Path, source_sha: str):
    if _sha(repo, 'HEAD') != source_sha:
        raise UserError('Source HEAD changed since this run started', 'stale_source')
    if not _clean(repo):
        raise UserError('Source has uncommitted changes; apply refused', 'dirty_source')


def apply_candidate(db, run_id: str, *, dry_run: bool = True, allow_unaccepted: bool = False,
                    candidate_id: str | None = None) -> dict:
    run = db.row('SELECT r.*,s.repo_root FROM runs r JOIN scopes s ON s.id=r.id WHERE r.id=?', (run_id,))
    if not run:
        raise UserError('Unknown run')
    candidate = db.row('SELECT * FROM candidates WHERE id=?', (candidate_id or run['current_candidate_id'],))
    if not candidate or candidate['run_id'] != run_id:
        raise UserError('The selected candidate does not belong to this run')
    if run['outcome'] != 'accepted' and not allow_unaccepted:
        raise UserError(f"Run outcome is {run['outcome']}; use --allow-unaccepted explicitly", 'unaccepted_candidate')
    if candidate['id'] != run['current_candidate_id'] and not allow_unaccepted:
        raise UserError('Only the accepted current candidate has current acceptance evidence', 'unaccepted_candidate')
    repo = Path(run['repo_root']).resolve(strict=True)
    _source_unchanged(repo, run['source_head_sha'])
    patch = diff(repo, run['source_head_sha'], candidate['commit_sha'])
    if not patch:
        raise UserError('Candidate contains no source changes to apply', 'empty_diff')
    git(repo, 'apply', '--check', '--binary', '-', input=patch)
    _source_unchanged(repo, run['source_head_sha'])
    if not dry_run:
        with db.transaction():
            db.event(run_id, 'apply.started', {'candidate_id': candidate['id'],
                                             'allow_unaccepted': allow_unaccepted, 'outcome': run['outcome']})
        git(repo, 'apply', '--binary', '-', input=patch)
        with db.transaction():
            db.event(run_id, 'apply.completed', {'candidate_id': candidate['id']})
    return {'run_id': run_id, 'candidate_id': candidate['id'], 'source_head_sha': run['source_head_sha'],
            'commit_sha': candidate['commit_sha'], 'dry_run': dry_run, 'applied': not dry_run,
            'outcome': run['outcome'], 'allow_unaccepted': allow_unaccepted,
            'patch': patch.decode('utf-8', errors='replace'), 'diff_sha256': digest(patch)}


def cleanup_worktree(db, worktree_id: str, *, dry_run: bool = True) -> dict:
    row = db.row('SELECT * FROM worktrees WHERE id=?', (worktree_id,))
    if not row:
        raise UserError('Unknown worktree ID', 'worktree_ownership')
    path, source = _owned(db, row)
    if not _clean(path, ignored=True):
        raise UserError('Worktree has user changes or ignored files; inspect it before cleanup', 'dirty_worktree')
    result = {'worktree_id': worktree_id, 'path': str(path), 'dry_run': dry_run, 'removed': False}
    if not dry_run:
        _worktree_state(db, worktree_id, row['scope_id'], 'removing')
        try:
            git(source, 'worktree', 'remove', str(path))
        except UserError:
            _worktree_state(db, worktree_id, row['scope_id'], 'failed')
            raise
        _worktree_state(db, worktree_id, row['scope_id'], 'removed')
        result['removed'] = True
    return result
