#!/usr/bin/env python3
"""
fleetps.py - Fleet-wide container and pod inventory
After Dark Systems - Ops Utils

Shows every running Docker container and k3s pod across all hosts in one table.

Usage:
  fleetps.py --all
  fleetps.py --host web-01 db-01
  fleetps.py --all --dc prod --json
"""

import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)

_COLLECT = r"""
# Docker containers
if command -v docker >/dev/null 2>&1; then
    docker ps --no-trunc --format '{{json .}}' 2>/dev/null | while IFS= read -r line; do
        printf "DOCKER %s\n" "$line"
    done
fi
# k3s pods
if command -v k3s >/dev/null 2>&1 || [ -f /usr/local/bin/k3s ]; then
    k3s kubectl get pods -A -o json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for p in d.get('items',[]):
    m=p['metadata']; s=p['status']
    cs=s.get('containerStatuses',[{}])
    img=cs[0].get('image','') if cs else ''
    print('K3S',json.dumps({'ns':m.get('namespace',''),'name':m.get('name',''),
        'status':s.get('phase',''),'image':img,'node':s.get('hostIP','')}))
" 2>/dev/null || true
fi
"""


def collect(host: dict, timeout: int, verbose: bool) -> dict:
    stdout, err = ssh_run(host, _COLLECT, timeout, verbose)
    if err:
        return {'host': host['name'], 'error': err, 'containers': [], 'pods': []}

    containers, pods = [], []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith('DOCKER '):
            try:
                raw = json.loads(line[7:])
                containers.append({
                    'name':    raw.get('Names', ''),
                    'image':   raw.get('Image', ''),
                    'status':  raw.get('Status', ''),
                    'ports':   raw.get('Ports', ''),
                    'id':      raw.get('ID', '')[:12],
                })
            except (json.JSONDecodeError, KeyError):
                pass
        elif line.startswith('K3S '):
            try:
                raw = json.loads(line[4:])
                pods.append(raw)
            except (json.JSONDecodeError, KeyError):
                pass

    return {'host': host['name'], 'error': None, 'containers': containers, 'pods': pods}


def print_table(results: list[dict]) -> None:
    total_c = total_p = 0
    print()
    print(Colors.bold(fmt('HOST', 22) + fmt('TYPE', 8) + fmt('NAME', 32) +
                      fmt('IMAGE', 42) + fmt('STATUS/PHASE', 18) + 'PORTS / NAMESPACE'))
    hr()
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 22) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue
        rows = (
            [('docker', c['name'], c['image'], c['status'], c['ports']) for c in r['containers']] +
            [('k3s',    p['name'], p['image'], p['status'], p['ns'])    for p in r['pods']]
        )
        if not rows:
            print(fmt(r['host'], 22) + Colors.warn('(no containers/pods)'))
            continue
        for i, (kind, name, image, status, extra) in enumerate(rows):
            host_col = r['host'] if i == 0 else ''
            kind_col = Colors.info('k3s') if kind == 'k3s' else Colors.ok('docker')
            # shorten image to fit
            if len(image) > 40:
                image = '…' + image[-39:]
            print(fmt(host_col, 22) + fmt(kind_col, 8) + fmt(name, 32) +
                  fmt(image, 42) + fmt(status, 18) + extra)
        total_c += len(r['containers'])
        total_p += len(r['pods'])
    hr()
    print(f"  {Colors.bold('Total:')} {Colors.ok(str(total_c))} docker containers  "
          f"{Colors.info(str(total_p))} k3s pods  across {len(results)} hosts\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Show all running containers and pods across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    log_info(f"Collecting from {len(hosts)} host(s)…")
    results = run_parallel(
        lambda h: collect(h, args.timeout, args.verbose),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps(results, indent=2))
    else:
        print_table(results)


if __name__ == '__main__':
    main()
