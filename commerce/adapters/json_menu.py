"""Send a complete external menu export without changing its observation time.

Run with Python's standard library only:
  python -m commerce.adapters.json_menu --base-url https://host/commerce \
      --connection CONNECTION_UUID --snapshot /path/to/export.json
Set COMMERCE_MENU_ADAPTER_SECRET in the adapter environment.
"""
import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError

from commerce.adapter_client import AdapterClient


def send_snapshot(client, snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('complete') is not True:
        raise ValueError('The external export must declare complete=true.')
    if len(json.dumps(snapshot, separators=(',', ':'), ensure_ascii=False).encode()) > 262144:
        raise ValueError('Menu export exceeds the 256 KiB atomic snapshot limit.')
    # Never fetch the next sequence or stamp "now" here: doing so would turn a
    # delayed file/retry into a newer, apparently fresh menu.
    return client.request('POST', 'catalog/snapshot/', snapshot)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Synchronize a complete JSON menu from an external application.')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--connection', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--snapshot', type=Path, help='Complete, atomically written export from the menu source.')
    mode.add_argument('--manifest', action='store_true', help='Read the authority generation and current sequence.')
    args = parser.parse_args(argv)
    secret = os.environ.get('COMMERCE_MENU_ADAPTER_SECRET')
    if not secret:
        parser.error('Set COMMERCE_MENU_ADAPTER_SECRET in the environment.')
    try:
        client = AdapterClient(args.base_url, args.connection, secret)
        result = client.request('GET', 'manifest/') if args.manifest else send_snapshot(client, json.loads(args.snapshot.read_text()))
    except HTTPError as exc:
        # Response details contain validation errors, never request credentials.
        parser.exit(1, f'Adapter request failed (HTTP {exc.code}): {exc.read(4096).decode(errors="replace")}\n')
    except (ValueError, OSError, URLError) as exc:
        parser.exit(1, f'Menu synchronization failed: {exc}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
