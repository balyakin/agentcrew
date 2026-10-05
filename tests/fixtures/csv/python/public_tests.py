import csv
import subprocess
import sys
from pathlib import Path

from orders_export import export_orders

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run([sys.executable, 'orders_cli.py', 'export-orders', *args], cwd=ROOT,
                          capture_output=True, text=True, check=False)


def test_empty_orders(tmp_path):
    output = tmp_path / 'empty.csv'
    export_orders([], output)
    assert list(csv.reader(output.open(encoding='utf-8', newline=''))) == [['id', 'total', 'currency']]


def test_sorted_orders(tmp_path):
    output = tmp_path / 'sorted.csv'
    export_orders([{'id': 'b', 'total': '2', 'currency': 'USD'},
                   {'id': 'a', 'total': '1', 'currency': 'EUR'}], output)
    assert [row['id'] for row in csv.DictReader(output.open(encoding='utf-8', newline=''))] == ['a', 'b']


def test_escaping(tmp_path):
    output = tmp_path / 'quoted.csv'
    data = [{'id': 'a', 'total': '1,000', 'currency': 'E"UR\nnew'}]
    export_orders(data, output)
    assert list(csv.DictReader(output.open(encoding='utf-8', newline=''))) == data


def test_refuses_overwrite(tmp_path):
    output = tmp_path / 'existing.csv'
    output.write_bytes(b'precious')
    result = cli('--output', str(output))
    assert result.returncode == 2
    assert result.stderr.strip()
    assert output.read_bytes() == b'precious'


def test_force_overwrite(tmp_path):
    output = tmp_path / 'existing.csv'
    output.write_text('old')
    result = cli('--output', str(output), '--force')
    assert result.returncode == 0
    assert list(csv.DictReader(output.open(encoding='utf-8', newline='')))[0]['id'] == '1'


def test_utf8_and_success(tmp_path):
    output = tmp_path / 'unicode.csv'
    export_orders([{'id': 'a', 'total': '1', 'currency': 'рубль'}], output)
    assert 'рубль' in output.read_text(encoding='utf-8')
    other = tmp_path / 'cli.csv'
    assert cli('--output', str(other)).returncode == 0


def test_write_failure_preserves_existing(tmp_path, monkeypatch):
    import os

    from orders_cli import main
    output = tmp_path / 'existing.csv'
    output.write_bytes(b'original bytes')
    def fail_replace(*_):
        raise OSError('controlled write failure')
    monkeypatch.setattr(os, 'replace', fail_replace)
    assert main(['export-orders', '--output', str(output), '--force']) != 0
    assert output.read_bytes() == b'original bytes'
