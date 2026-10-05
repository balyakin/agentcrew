"""Executable Codex contract double for real orchestration tests only."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
if '--version' in args:
    print('codex-cli 0.160.0-test')
    raise SystemExit(0)
if '--help' in args:
    print('login\nstatus\n--json --output-schema --output-last-message -C --sandbox '
          'read-only workspace-write --ask-for-approval --model')
    raise SystemExit(0)
if args[:2] == ['login', 'status']:
    print('Logged in using a test account')
    raise SystemExit(0)

prompt = sys.stdin.read()
match = re.search(r'AGENTCREW_KIND:([a-z-]+)', prompt)
kind = match[1] if match else 'unknown'
root = Path.cwd()
template = os.environ['CSV_TEMPLATE']
mode = os.environ.get('CSV_MODE', 'normal')
fixture = Path(os.environ['CSV_FIXTURES'])
record = json.dumps({'kind': kind, 'cwd': str(root), 'pid': os.getpid()}) + '\n'
descriptor = os.open(os.environ['CSV_CALLS'], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
try:
    os.write(descriptor, record.encode())
finally:
    os.close(descriptor)

names = ['empty_orders', 'sorted_orders', 'escaping', 'refuses_overwrite', 'force_overwrite',
         'utf8_and_success', 'write_failure_preserves_existing', 'status_keeps_existing_behavior']
descriptions = ['Empty list gives only id,total,currency header.', 'Rows are sorted by id.',
                'Commas, quotes and newlines round-trip.', 'Existing output is preserved without force; CLI exits 2.',
                'Force replaces an existing output after successful write.', 'UTF-8 output and success exit code 0.',
                'Write errors are nonzero and preserve existing output.', 'Existing source API and status CLI still work.']
requirements = []
for index, (name, description) in enumerate(zip(names, descriptions, strict=True), 1):
    filename = ('tests/test_cli.py' if index == 8 else 'tests/test_export.py') if template == 'python-cli' else (
        'tests/cli.test.ts' if index == 8 else 'tests/export.test.ts')
    test_name = 'test_' + name if template == 'python-cli' else name
    identifier = filename + '::' + test_name
    if mode == 'unknown' and index == 3:
        identifier = filename + '::never_executed'
    requirements.append({'id': f'A{index}', 'description': description,
                         'verification': {'kind': 'test', 'check': 'regression' if index == 8 else 'unit',
                                          'test_id': identifier}})

if kind == 'plan':
    result = {'schema_version': 1, 'summary': 'Implement and verify all CSV conditions',
              'requirements': requirements, 'work_items': [{'id': 'W1', 'role': 'implementer',
                  'description': 'Add CSV command, tests and examples without runtime dependencies', 'depends_on': []}],
              'constraints': ['Preserve the existing API and status command', 'No runtime dependencies'], 'questions': []}
elif kind in {'implement', 'revise'}:
    source = fixture / ('python' if template == 'python-cli' else 'typescript')
    files = ({'orders_export.py': 'orders_export.py', 'orders_cli.py': 'orders_cli.py',
              'public_tests.py': 'tests/test_export.py'} if template == 'python-cli' else
             {'export.ts': 'src/export.ts', 'cli.ts': 'src/cli.ts', 'export.test.ts': 'tests/export.test.ts'})
    for original, destination in files.items():
        contents = (source / original).read_text()
        if mode == 'revise' and kind == 'implement' and original == 'orders_export.py':
            contents = contents.replace("writer.writerow(['id', 'total', 'currency'])",
                                        "writer.writerow(['id', 'total', 'wrong_header'])")
        target = root / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    command = 'python3 orders_cli.py' if template == 'python-cli' else 'node --experimental-strip-types src/cli.ts'
    (root / 'README.md').write_text('# Orders tool\n\nExisting status: `' + command + ' status`.\n\n'
                                  'Export: `' + command + ' export-orders --output orders.csv`.\n'
                                  'Use `--force` to replace an existing output. Writes are atomic.\n'
                                  'CSV is UTF-8, sorted by id, with id,total,currency columns.\n')
    tracked = subprocess.run(['git', 'diff', '--name-only', '-z', 'HEAD', '--'], capture_output=True, check=True).stdout
    untracked = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '-z'],
                               capture_output=True, check=True).stdout
    changed = sorted({os.fsdecode(path) for path in (tracked + untracked).split(b'\x00') if path})
    result = {'schema_version': 1, 'summary': 'Implemented CSV, behavioral tests and README examples',
              'changed_files': changed, 'addressed_requirements': [requirement['id'] for requirement in requirements],
              'remaining_requirements': [], 'limitations': []}
else:
    if mode == 'reviewer-failed' and kind == 'tests':
        print('controlled reviewer process failure', file=sys.stderr)
        raise SystemExit(9)
    if mode == 'reviewer-invalid' and kind == 'tests':
        result = {'unexpected': 'invalid reviewer output'}
    else:
        result = {'schema_version': 1, 'summary': 'Independent fixture review completed',
                  'findings': [], 'limitations': ['Test double verifies orchestration, not real model quality.']}

Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(result))
print(json.dumps({'type': 'thread.started', 'thread_id': 'csv-test-' + str(os.getpid())}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 2}}))
