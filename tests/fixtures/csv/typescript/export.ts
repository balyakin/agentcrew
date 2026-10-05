import { closeSync, linkSync, mkdtempSync, openSync, renameSync, rmdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { Order } from './orders.ts';

function cell(value: unknown): string {
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function exportOrders(orders: Order[], output: string, force = false): void {
  const directory = mkdtempSync(join(dirname(output), '.orders-'));
  const temporary = join(directory, 'output.csv');
  try {
    const rows = [['id', 'total', 'currency'], ...[...orders].sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
      .map(order => [order.id, order.total, order.currency])];
    const contents = rows.map(row => row.map(cell).join(',')).join('\r\n') + '\r\n';
    const descriptor = openSync(temporary, 'wx');
    try { writeFileSync(descriptor, contents, { encoding: 'utf8' }); }
    finally { closeSync(descriptor); }
    if (force) renameSync(temporary, output);
    else linkSync(temporary, output);
  } finally {
    try { unlinkSync(temporary); } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }
    rmdirSync(directory);
  }
}
