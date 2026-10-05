"""A test-provider implementation fixture, never used as a production adapter."""

import csv
import os
import tempfile
from pathlib import Path


def export_orders(orders, output, force=False):
    output = Path(output)
    if not force and os.path.lexists(output):
        raise FileExistsError(f'Output already exists: {output}; use --force')
    descriptor, temporary = tempfile.mkstemp(prefix='.orders-', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['id', 'total', 'currency'])
            for order in sorted(orders, key=lambda item: item['id']):
                writer.writerow([order['id'], order['total'], order['currency']])
        if force:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
