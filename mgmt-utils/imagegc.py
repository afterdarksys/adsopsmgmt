#!/usr/bin/env python3
"""
imagegc.py - Fleet-wide Docker image garbage collection
After Dark Systems - Ops Utils

Finds and removes dangling images and images older than N days that have
no running container. Dry-run by default — pass --execute to actually delete.

Usage:
  imagegc.py --all                          # dry-run: show what would be removed
  imagegc.py --all --older-than 30          # also target images older than 30 days
  imagegc.py --all --execute                # actually remove
  imagegc.py --host web-01 --execute --older-than 14
"""

import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_warn, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)

_DRY_RUN_SCRIPT = r"""
# Dangling images (untagged, not referenced)
DANGLING=$(docker images -f dangling=true --format '{{.ID}} {{.Size}} {{.CreatedAt}}' 2>/dev/null)
echo "--- DANGLING ---"
echo "$DANGLING"

# All images with creation date (to detect old ones)
echo "--- ALL ---"
docker images --format '{{.ID}} {{.Repository}} {{.Tag}} {{.Size}} {{.CreatedAt}}' 2>/dev/null

# Running image IDs (to exclude)
echo "--- RUNNING ---"
docker ps --format '{{.Image}}' 2>/dev/null | sort -u
"""


def _execute_script(older_than: int | None) -> str:
    age_clause = ""
    if older_than is not None:
        age_clause = f"""
# Remove images older than {older_than} days with no running container
RUNNING_IDS=$(docker ps --format '{{{{.ImageID}}}}' 2>/dev/null | sort -u)
CUTOFF=$(date -d '{older_than} days ago' +%s 2>/dev/null || \
         python3 -c "import time; print(int(time.time()) - {older_than}*86400)")
docker images --format '{{{{.ID}}}} {{{{.CreatedAt}}}}' 2>/dev/null | while read -r id created; do
    img_ts=$(date -d "$created" +%s 2>/dev/null || \
             python3 -c "from datetime import datetime; import sys; \
             print(int(datetime.fromisoformat('$created'.replace(' +0000','').replace(' UTC','')).timestamp()))" 2>/dev/null || echo 0)
    if [ "$img_ts" -lt "$CUTOFF" ] 2>/dev/null; then
        if ! echo "$RUNNING_IDS" | grep -q "$id"; then
            docker rmi "$id" 2>/dev/null && echo "REMOVED_OLD $id" || true
        fi
    fi
done
"""
    return f"""
echo "PRUNE_START"
docker image prune -f 2>/dev/null | tail -1
echo "PRUNE_DONE"
{age_clause}
"""


def collect_dry(host: dict, timeout: int, verbose: bool, older_than: int | None) -> dict:
    stdout, err = ssh_run(host, _DRY_RUN_SCRIPT, timeout, verbose)
    if err:
        return {'host': host['name'], 'error': err,
                'dangling': [], 'old_images': [], 'space_mb': 0.0}

    section = None
    dangling, all_images, running = [], [], set()

    for line in stdout.splitlines():
        line = line.strip()
        if line == '--- DANGLING ---':  section = 'dangling'; continue
        if line == '--- ALL ---':       section = 'all';      continue
        if line == '--- RUNNING ---':   section = 'running';  continue

        if not line:
            continue
        if section == 'dangling':
            parts = line.split(None, 2)
            if len(parts) >= 2:
                dangling.append({'id': parts[0][:12], 'size': parts[1] if len(parts) > 1 else ''})
        elif section == 'all':
            parts = line.split(None, 4)
            if len(parts) >= 5:
                all_images.append({'id': parts[0][:12], 'repo': parts[1],
                                   'tag': parts[2], 'size': parts[3], 'created': parts[4]})
        elif section == 'running':
            running.add(line.split(':')[-1][:12])
            running.add(line)

    old_images = []
    if older_than is not None:
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than)
        for img in all_images:
            if img['id'] in running or img['repo'] in running:
                continue
            try:
                # Try parsing 'YYYY-MM-DD HH:MM:SS +ZZZZ'
                dt = datetime.strptime(img['created'][:19], '%Y-%m-%d %H:%M:%S')
                dt = dt.replace(tzinfo=timezone.utc)
                if dt < cutoff:
                    old_images.append(img)
            except ValueError:
                pass

    return {
        'host': host['name'], 'error': None,
        'dangling': dangling, 'old_images': old_images,
        'space_mb': 0.0,
    }


def execute(host: dict, timeout: int, verbose: bool, older_than: int | None) -> dict:
    script = _execute_script(older_than)
    stdout, err = ssh_run(host, script, max(timeout, 120), verbose)
    if err:
        return {'host': host['name'], 'error': err, 'removed': 0, 'space_freed': ''}

    removed = stdout.count('REMOVED_OLD')
    space = ''
    for line in stdout.splitlines():
        if 'reclaimed' in line.lower():
            space = line.strip()
            break
    return {'host': host['name'], 'error': None, 'removed': removed, 'space_freed': space}


def print_dry_table(results: list[dict], older_than: int | None) -> None:
    total_d = total_o = 0
    print()
    age_note = f" + images older than {older_than}d" if older_than else ""
    print(Colors.bold(f"Dry run — dangling{age_note} images that would be removed:"))
    print()
    print(Colors.bold(fmt('HOST', 22) + fmt('TYPE', 12) + fmt('IMAGE ID', 14) +
                      fmt('SIZE', 10) + 'REPO:TAG'))
    hr(80)
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 22) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue
        rows: list[tuple] = (
            [('dangling', d['id'], d.get('size', ''), '<none>:<none>') for d in r['dangling']] +
            [('old',      o['id'], o['size'], f"{o['repo']}:{o['tag']}")  for o in r['old_images']]
        )
        if not rows:
            print(fmt(r['host'], 22) + Colors.ok('nothing to remove'))
            continue
        for i, (kind, img_id, size, repo_tag) in enumerate(rows):
            host_col = r['host'] if i == 0 else ''
            kind_col = Colors.warn(kind)
            print(fmt(host_col, 22) + fmt(kind_col, 12) + fmt(img_id, 14) +
                  fmt(size, 10) + repo_tag)
        total_d += len(r['dangling'])
        total_o += len(r['old_images'])
    hr(80)
    print(f"  {Colors.bold('Would remove:')} {total_d} dangling + {total_o} old image(s)")
    print(f"  Re-run with {Colors.bold('--execute')} to actually delete.\n")


def print_exec_table(results: list[dict]) -> None:
    print()
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(f"{fmt(r['host'], 22)} {Colors.warn('UNREACHABLE')}  {r['error']}")
        else:
            freed = f"  {r['space_freed']}" if r['space_freed'] else ''
            print(f"{fmt(r['host'], 22)} {Colors.ok('done')}  "
                  f"{r['removed']} old image(s) removed{freed}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Garbage-collect Docker images across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--older-than', type=int, default=None, metavar='DAYS',
                        help='Also remove images older than N days (no running container)')
    parser.add_argument('--execute', action='store_true',
                        help='Actually remove images (default: dry run)')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    if args.execute:
        log_warn(f"EXECUTE MODE — removing images on {len(hosts)} host(s)…")
        results = run_parallel(
            lambda h: execute(h, args.timeout, args.verbose, args.older_than),
            hosts, args.concurrency)
        if args.json_out:
            print(json.dumps(results, indent=2))
        else:
            print_exec_table(results)
    else:
        log_info(f"Dry run on {len(hosts)} host(s)…")
        results = run_parallel(
            lambda h: collect_dry(h, args.timeout, args.verbose, args.older_than),
            hosts, args.concurrency)
        if args.json_out:
            print(json.dumps(results, indent=2))
        else:
            print_dry_table(results, args.older_than)


if __name__ == '__main__':
    main()
