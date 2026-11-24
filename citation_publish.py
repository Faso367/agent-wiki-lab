"""Replace private benchmark page addresses in raw attempts with public evidence identifiers."""
import argparse
import json
from pathlib import Path
import re

from scale_xerj import save


def public_attempts(rows, inventory):
    site = inventory['site']
    reverse = {str(page): doc for doc, page in inventory['pages'].items()}
    cloud_url = re.compile(r'https://' + re.escape(site) + r'/wiki/spaces/' +
                           re.escape(inventory['space']) + r'/pages/(\d+)(?:#:~:text=([^\s)]+))?')
    local_url = re.compile(r'http://127\.0\.0\.1:\d+/(dsid_[a-zA-Z0-9_-]+)\.html(#[-\w]+)?')

    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, str):
            value = cloud_url.sub(lambda m: reverse[m[1]] + ('#:~:text=' + m[2] if m[2] else ''), value)
            value = local_url.sub(lambda m: 'confluence:' + m[1] + (m[2] or ''), value)
            if site in value or 'http://127.0.0.1:' in value and '.html#' in value:
                raise ValueError('Unrecognized private benchmark address')
        return value

    return redact(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    attempts = json.loads((args.run / 'attempts.json').read_text())
    inventory = json.loads((args.run / 'inventory.json').read_text())
    save(args.output, public_attempts(attempts, inventory))
