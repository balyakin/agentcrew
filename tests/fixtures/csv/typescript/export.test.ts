import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { exportOrders } from '../src/export.ts';

const root = fileURLToPath(new URL('..', import.meta.url));
function cli(...args: string[]) {
  return spawnSync(process.execPath, ['--experimental-strip-types', 'src/cli.ts', 'export-orders', ...args],
                   { cwd: root, encoding: 'utf8' });
}
function output(t: { after: (callback: () => void) => void }): string {
  const directory = mkdtempSync(join(tmpdir(), 'csv-fixture-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return join(directory, 'orders.csv');
}

test('tests/export.test.ts::empty_orders', t => {
  const file = output(t); exportOrders([], file);
  assert.equal(readFileSync(file, 'utf8'), 'id,total,currency\r\n');
});
test('tests/export.test.ts::sorted_orders', t => {
  const file = output(t);
  exportOrders([{ id: 'b', total: '2', currency: 'USD' }, { id: 'a', total: '1', currency: 'EUR' }], file);
  assert.equal(readFileSync(file, 'utf8'), 'id,total,currency\r\na,1,EUR\r\nb,2,USD\r\n');
});
test('tests/export.test.ts::escaping', t => {
  const file = output(t); exportOrders([{ id: 'a', total: '1,000', currency: 'E"UR\nnew' }], file);
  assert.equal(readFileSync(file, 'utf8'), 'id,total,currency\r\na,"1,000","E""UR\nnew"\r\n');
});
test('tests/export.test.ts::refuses_overwrite', t => {
  const file = output(t); writeFileSync(file, 'precious');
  const result = cli('--output', file);
  assert.equal(result.status, 2); assert.ok(result.stderr.trim()); assert.equal(readFileSync(file, 'utf8'), 'precious');
});
test('tests/export.test.ts::force_overwrite', t => {
  const file = output(t); writeFileSync(file, 'old');
  assert.equal(cli('--output', file, '--force').status, 0);
  assert.ok(readFileSync(file, 'utf8').startsWith('id,total,currency\r\n1,'));
});
test('tests/export.test.ts::utf8_and_success', t => {
  const file = output(t); exportOrders([{ id: 'a', total: '1', currency: 'рубль' }], file);
  assert.ok(readFileSync(file, 'utf8').includes('рубль'));
});
test('tests/export.test.ts::write_failure_preserves_existing', t => {
  const file = output(t); writeFileSync(file, 'original bytes');
  const script = `import fs from 'node:fs'; import { syncBuiltinESMExports } from 'node:module';
    fs.renameSync = () => { throw new Error('controlled write failure'); }; syncBuiltinESMExports();
    const { main } = await import('./src/cli.ts'); process.exitCode = main(['export-orders', '--output', ${JSON.stringify(file)}, '--force']);`;
  const result = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', script],
                           { cwd: root, encoding: 'utf8' });
  assert.notEqual(result.status, 0); assert.equal(readFileSync(file, 'utf8'), 'original bytes');
});
