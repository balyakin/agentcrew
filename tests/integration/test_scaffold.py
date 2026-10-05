"""Both new-project baselines run real code and fresh machine-readable tests."""

import importlib
import json
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET

import pytest

from agentcrew.models import UserError


@pytest.fixture
def scaffold_module():
    assert importlib.util.find_spec('agentcrew.develop.scaffold'), 'Scaffold implementation is missing'
    return importlib.import_module('agentcrew.develop.scaffold')


@pytest.mark.parametrize('template', ['python-cli', 'typescript-cli'])
def test_scaffold_runs_cli_tests_and_clean_git(tmp_path, scaffold_module, template):
    spec = tmp_path / 'task.md'
    spec.write_text('Add CSV export-orders after creating the baseline.\n')
    directory = tmp_path / 'orders project'
    assert scaffold_module.scaffold(directory, template, spec) == directory.resolve()
    if template == 'python-cli':
        argv = [sys.executable, 'orders_cli.py', 'status']
    else:
        argv = ['node', '--experimental-strip-types', 'src/cli.ts', 'status']
    status = subprocess.run(argv, cwd=directory, capture_output=True, text=True, check=False)
    assert status.returncode == 0
    assert status.stdout.strip() == 'orders: 2'
    unsupported = subprocess.run([*argv[:-1], 'export-orders'], cwd=directory, capture_output=True, check=False)
    assert unsupported.returncode == 2
    config = tomllib.loads((directory / '.agentcrew.toml').read_text())
    assert config['checks'][0]['result_format'] == 'junit'
    for check in config['checks']:
        result = subprocess.run(check['argv'], cwd=directory, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    report = directory / config['checks'][0]['result_file']
    cases = list(ET.parse(report).iter('testcase'))
    assert cases and all(not list(case.iter('failure')) for case in cases)
    clean = subprocess.run(['git', 'status', '--porcelain'], cwd=directory, capture_output=True, check=True)
    assert clean.stdout == b''
    committed = subprocess.run(['git', 'ls-files', '.agentcrew.toml'], cwd=directory,
                               capture_output=True, check=True)
    assert committed.stdout == b'.agentcrew.toml\n'
    if template == 'typescript-cli':
        assert not json.loads((directory / 'package.json').read_text()).get('dependencies')


@pytest.mark.parametrize('problem', ['missing-spec', 'unknown-template', 'occupied', 'missing-runtime'])
def test_scaffold_preflight_leaves_destination_untouched(tmp_path, scaffold_module, problem, monkeypatch):
    directory = tmp_path / 'target'
    spec = tmp_path / 'task.md'
    spec.write_text('Build a small CLI.')
    template = 'python-cli'
    if problem == 'missing-spec':
        spec.unlink()
    elif problem == 'unknown-template':
        template = 'unrecognized'
    elif problem == 'occupied':
        directory.mkdir()
        (directory / 'keep').write_text('valuable')
    elif problem == 'missing-runtime':
        template = 'typescript-cli'
        real_which = scaffold_module.shutil.which
        monkeypatch.setattr(scaffold_module.shutil, 'which', lambda name: None if name == 'node' else real_which(name))
    with pytest.raises(UserError):
        scaffold_module.scaffold(directory, template, spec)
    if problem == 'occupied':
        assert list(directory.iterdir()) == [directory / 'keep']
        assert (directory / 'keep').read_text() == 'valuable'
    else:
        assert not directory.exists()


def test_scaffold_accepts_empty_directory(tmp_path, scaffold_module):
    directory = tmp_path / 'empty'
    directory.mkdir()
    spec = tmp_path / 'task.md'
    spec.write_text('Keep status and add another command.')
    scaffold_module.scaffold(directory, 'python-cli', spec)
    assert (directory / 'orders_cli.py').is_file()


def test_scaffold_rejects_nul_spec_before_creating_directory(tmp_path, scaffold_module):
    directory = tmp_path / 'target'
    spec = tmp_path / 'task.md'
    spec.write_bytes(b'valid text\x00hidden instructions')
    with pytest.raises(UserError):
        scaffold_module.scaffold(directory, 'python-cli', spec)
    assert not directory.exists()
