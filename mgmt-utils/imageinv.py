#!/usr/bin/env python3
"""
imageinv.py - Fleet-wide Docker image inventory
After Dark Systems - Ops Utils

Lists every Docker image on every host — repo, tag, size, age.
Great for spotting stale images, version drift, and disk hogs.

Usage:
  imageinv.py --all
  imageinv.py --host web-01
  imageinv.py --all --repo nginx          # filter by repo substring
  imageinv.py --all --older-than 30       # only images older than N days
  imageinv.py --all --json
"""

import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)

_COLLECT = r"""
docker images --no-trunc --format '{{json .}}' 2>/dev/null
"""


def _parse_size(s: str) -> float:
    """Convert Docker size string (e.g. '1.23GB', '456MB') to MB float."""
    s = s.strip()
    for unit, mult in (('GB', 1024), ('MB', 1), ('KB', 1/1024), ('B', 1/1048576)):
        if s.upper().endswith(unit):
            try:
                return float(s[:-len(unit)]) * mult
            except ValueError:
                return 0.0
    return 0.0


def collect(host: dict, timeout: int, verbose: bool) -> dict:
    stdout, err = ssh_run(host, _COLLECT, timeout, verbose)
    if err:
        return {'host': host['name'], 'error': err, 'images': []}

    images = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            images.append({
                'repo':    raw.get('Repository', '<none>'),
                'tag':     raw.get('Tag', '<none>'),
                'id':      raw.get('ID', '')[:12],
                'created': raw.get('CreatedAt', ''),
                'size':    raw.get('Size', '0B'),
                'size_mb': _parse_size(raw.get('Size', '0B')),
            })
        except (json.JSONDecodeError, KeyError):
            pass

    images.sort(key=lambda i: i['size_mb'], reverse=True)
    return {'host': host['name'], 'error': None, 'images': images}


def print_table(results: list[dict], repo_filter: str | None, older_than: int | None) -> None:
    from datetime import datetime, timezone
    total_images = 0
    total_mb = 0.0

    print()
    print(Colors.bold(fmt('HOST', 20) + fmt('REPO:TAG', 52) +
                      fmt('IMAGE ID', 14) + fmt('SIZE', 10) + 'CREATED'))
    hr()
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 20) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue

        imgs = r['images']
        if repo_filter:
            imgs = [i for i in imgs if repo_filter.lower() in i['repo'].lower()]
        if older_than is not None:
            now = datetime.now(timezone.utc)
            def _age_days(created: str) -> int:
                for fmt_s in ('%Y-%m-%d %H:%M:%S %z', '%Y-%m-%dT%H:%M:%SZ'):
                    try:
                        dt = datetime.strptime(created[:25], fmt_s[:len(created[:25])])
                        if dt.tzinfo is None:
                            import dateutil.parser  # type: ignore
                            dt = dateutil.parser.parse(created)
                        return (now - dt).days
                    except Exception:
                        pass
                return 0
            imgs = [i for i in imgs if _age_days(i['created']) >= older_than]

        if not imgs:
            print(fmt(r['host'], 20) + Colors.warn('(no matching images)'))
            continue

        for idx, img in enumerate(imgs):
            host_col  = r['host'] if idx == 0 else ''
            repo_tag  = f"{img['repo']}:{img['tag']}"
            if len(repo_tag) > 50:
                repo_tag = '…' + repo_tag[-49:]
            size_col = img['size']
            if img['size_mb'] > 500:
                size_col = Colors.warn(size_col)
            print(fmt(host_col, 20) + fmt(repo_tag, 52) + fmt(img['id'], 14) +
                  fmt(size_col, 10) + img['created'][:19])
            total_images += 1
            total_mb += img['size_mb']

    hr()
    print(f"  {Colors.bold('Total:')} {total_images} image(s)  "
          f"{Colors.warn(f'{total_mb/1024:.1f} GB')} across {len(results)} hosts\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Inventory Docker images across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--repo', default=None, metavar='STR',
                        help='Filter by repository name substring')
    parser.add_argument('--older-than', type=int, default=None, metavar='DAYS',
                        help='Only show images older than N days')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    log_info(f"Collecting images from {len(hosts)} host(s)…")
    results = run_parallel(
        lambda h: collect(h, args.timeout, args.verbose),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps(results, indent=2))
    else:
        print_table(results, args.repo, args.older_than)


if __name__ == '__main__':
    main()
