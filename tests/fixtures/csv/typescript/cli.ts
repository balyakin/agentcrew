import { pathToFileURL } from 'node:url';
import { getOrders } from './orders.ts';
import { exportOrders } from './export.ts';

export function main(argv: string[] = process.argv.slice(2)): number {
  if (argv.length === 1 && argv[0] === 'status') {
    console.log(`orders: ${getOrders().length}`);
    return 0;
  }
  if (argv.length === 1 && ['--help', '-h'].includes(argv[0])) {
    console.log('Usage: orders status | export-orders --output PATH [--force]');
    return 0;
  }
  if (argv[0] !== 'export-orders') {
    console.error('Unknown command; use --help');
    return 2;
  }
  let output: string | undefined;
  let force = false;
  for (let index = 1; index < argv.length; index++) {
    if (argv[index] === '--force') force = true;
    else if (argv[index] === '--output' && index + 1 < argv.length && !output) output = argv[++index];
    else { console.error('Usage: export-orders --output PATH [--force]'); return 2; }
  }
  if (!output) { console.error('--output PATH is required'); return 2; }
  try {
    exportOrders(getOrders(), output, force);
    return 0;
  } catch (error) {
    console.error(`Cannot export orders: ${String(error)}`);
    return (error as NodeJS.ErrnoException).code === 'EEXIST' ? 2 : 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) process.exitCode = main();
