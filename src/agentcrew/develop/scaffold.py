"""Runnable, dependency-light baselines for the same develop workflow."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from agentcrew.git_snapshot import git
from agentcrew.models import UserError

PYTHON_FILES = {
    'orders.py': '''
        """Existing orders API. Keep this behavior when adding commands."""
        import json
        from pathlib import Path


        def get_orders(path=None):
            source = Path(path) if path is not None else Path(__file__).parent / "data/orders.json"
            return json.loads(source.read_text(encoding="utf-8"))
    ''',
    'orders_cli.py': '''
        """Small existing CLI; export-orders is the development task."""
        import argparse

        from orders import get_orders


        def main(argv=None):
            parser = argparse.ArgumentParser(prog="orders")
            commands = parser.add_subparsers(dest="command", required=True)
            commands.add_parser("status", help="Show the number of existing orders")
            args = parser.parse_args(argv)
            if args.command == "status":
                print(f"orders: {len(get_orders())}")
            return 0

        if __name__ == "__main__":
            raise SystemExit(main())
    ''',
    'tests/test_cli.py': '''
        import subprocess
        import sys
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[1]

        def test_status_keeps_existing_behavior():
            result = subprocess.run([sys.executable, "orders_cli.py", "status"], cwd=ROOT,
                                    capture_output=True, text=True, check=False)
            assert result.returncode == 0
            assert result.stdout == "orders: 2\\n"
    ''',
    'pyproject.toml': '''
        [build-system]
        requires = ["setuptools>=75"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "orders-tool"
        version = "0.1.0"
        requires-python = ">=3.12"
        dependencies = []

        [project.optional-dependencies]
        dev = ["pytest>=8"]

        [project.scripts]
        orders = "orders_cli:main"

        [tool.setuptools]
        py-modules = ["orders", "orders_cli"]

        [tool.pytest.ini_options]
        testpaths = ["tests"]
    ''',
}

TYPESCRIPT_FILES = {
    'src/orders.ts': '''
        import { readFileSync } from 'node:fs';
        import { fileURLToPath } from 'node:url';

        export type Order = { id: string; total: string; currency: string };

        // Existing API. Optional path supports controlled fixture inputs.
        export function getOrders(path = fileURLToPath(new URL('../data/orders.json', import.meta.url))): Order[] {
          return JSON.parse(readFileSync(path, 'utf8'));
        }
    ''',
    'src/cli.ts': '''
        import { pathToFileURL } from 'node:url';
        import { getOrders } from './orders.ts';

        export function main(argv: string[] = process.argv.slice(2)): number {
          if (argv.length === 1 && argv[0] === 'status') {
            console.log(`orders: ${getOrders().length}`);
            return 0;
          }
          if (argv.length === 1 && ['--help', '-h'].includes(argv[0])) {
            console.log('Usage: orders status');
            return 0;
          }
          console.error('Usage: orders status (unknown or missing command)');
          return 2;
        }

        if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
          process.exitCode = main();
        }
    ''',
    'tests/cli.test.ts': '''
        import { strict as assert } from 'node:assert';
        import { spawnSync } from 'node:child_process';
        import { fileURLToPath } from 'node:url';
        import test from 'node:test';

        const root = fileURLToPath(new URL('..', import.meta.url));

        test('tests/cli.test.ts::status_keeps_existing_behavior', () => {
          const result = spawnSync(process.execPath, ['--experimental-strip-types', 'src/cli.ts', 'status'],
                                   { cwd: root, encoding: 'utf8' });
          assert.equal(result.status, 0);
          assert.equal(result.stdout, 'orders: 2\\n');
        });
    ''',
}


def _environment(template: str):
    if not shutil.which('git'):
        raise UserError('Git is required before creating a project', 'environment_missing')
    if template == 'python-cli':
        if not Path(sys.executable).is_file() or sys.version_info < (3, 12) or not importlib.util.find_spec('pytest'):
            raise UserError('Python 3.12+ and pytest are required; install AgentCrew with its dev extra',
                            'environment_missing')
    else:
        if not shutil.which('node'):
            raise UserError('Node 22.18+ is required before creating this project', 'environment_missing')
        try:
            result = subprocess.run(['node', '--version'], capture_output=True, text=True,
                                    timeout=10, check=False)
            version = tuple(int(part) for part in result.stdout.strip().removeprefix('v').split('.'))
            if result.returncode or len(version) != 3 or version < (22, 18, 0):
                raise ValueError('unsupported Node version')
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise UserError('Node 22.18+ with native TypeScript stripping is required', 'environment_missing') from exc


def scaffold(directory: Path, template: str, spec: Path) -> Path:
    if template not in {'python-cli', 'typescript-cli'}:
        raise UserError('Unknown template; choose python-cli or typescript-cli', 'template_invalid')
    try:
        from .workflow import read_spec

        read_spec(Path(spec))
        requested = Path(directory).absolute()
        was_present = requested.exists()
        if requested.is_symlink() or (was_present and
                                     (not requested.is_dir() or any(requested.iterdir()))):
            raise UserError('New project directory must be absent or empty', 'directory_occupied')
        root = requested.resolve()
        _environment(template)
        ancestor = root.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        try:
            git(ancestor, 'rev-parse', '--show-toplevel')
        except UserError as exc:
            if exc.code != 'git_error':
                raise
        else:
            raise UserError('Create the new project outside an existing Git repository', 'directory_occupied')
        root.mkdir(parents=True, exist_ok=was_present)
        files = dict(PYTHON_FILES if template == 'python-cli' else TYPESCRIPT_FILES)
        files['data/orders.json'] = json.dumps([
            {'id': '2', 'total': '19.95', 'currency': 'USD'},
            {'id': '1', 'total': '12.50', 'currency': 'EUR'},
        ], indent=2) + '\n'
        files['.gitignore'] = '.venv/\n__pycache__/\n.pytest_cache/\n*.egg-info/\n.agentcrew-results/*.xml\nnode_modules/\n'
        files['.agentcrew-results/.gitkeep'] = ''
        if template == 'python-cli':
            cli = 'python3 orders_cli.py status'
            checks = [sys.executable, '-m', 'pytest', '-q', '--junitxml=.agentcrew-results/unit.xml']
            setup = 'python3 -m venv .venv\n.venv/bin/python -m pip install -e ".[dev]"'
            tests = '.venv/bin/python -m pytest -q --junitxml=.agentcrew-results/unit.xml'
            source = '`orders.py:get_orders(path=None)` reads `data/orders.json`.'
        else:
            cli = 'node --experimental-strip-types src/cli.ts status'
            checks = ['node', '--experimental-strip-types', '--test', '--test-reporter=junit',
                      '--test-reporter-destination=.agentcrew-results/unit.xml']
            setup = 'node --version  # requires Node 22.18 or newer; no install step'
            tests = 'npm test'
            source = '`src/orders.ts:getOrders(path?)` reads `data/orders.json`.'
            files['package.json'] = json.dumps({'name': 'orders-tool', 'version': '0.1.0', 'type': 'module',
                                               'private': True, 'engines': {'node': '>=22.18.0'},
                                               'scripts': {'status': cli, 'test': ' '.join(checks)}}, indent=2) + '\n'
        files['README.md'] = (f'# Orders tool\n\nRunnable baseline for a development task.\n\n'
                              f'```sh\n{setup}\n{cli}\n{tests}\n```\n\n'
                              f'{source} The existing `status` command reports the number of orders.\n'
                              'CSV export is a task for the agent and is not implemented in this baseline.\n'
                              'Test results are written to `.agentcrew-results/unit.xml`.\n')
        files['.agentcrew.toml'] = ('schema_version = 1\nlanguage = "en"\ndefault_provider = "codex"\n\n'
                                   '[[checks]]\nname = "unit"\n'
                                   f'argv = {json.dumps(checks)}\n'
                                   'timeout_seconds = 180\nresult_format = "junit"\n'
                                   'result_file = ".agentcrew-results/unit.xml"\n')
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as handle:
                handle.write(dedent(content).lstrip('\n'))
        git(root, 'init', '-b', 'main')
        git(root, 'add', '--all', '--', '.')
        git(root, '-c', 'user.name=AgentCrew', '-c', 'user.email=agentcrew@local',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'AgentCrew runnable project baseline')
        return root
    except (OSError, UnicodeError) as exc:
        raise UserError(f'Cannot create project; preserve its directory and inspect it: {exc}', 'scaffold_error') from exc
