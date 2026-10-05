import argparse
import sys

from orders import get_orders
from orders_export import export_orders


def main(argv=None):
    parser = argparse.ArgumentParser(prog='orders')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Show the number of existing orders')
    export = commands.add_parser('export-orders', help='Export orders as UTF-8 CSV')
    export.add_argument('--output', required=True)
    export.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'status':
        print(f'orders: {len(get_orders())}')
        return 0
    try:
        export_orders(get_orders(), args.output, args.force)
        return 0
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        print(f'Cannot export orders: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
