#!/usr/bin/env python3
"""
portmap.py - Fleet-wide port inventory
After Dark Systems - Ops Utils

Lists every listening TCP port across all hosts — system processes and
Docker-mapped ports — in one table. Useful for spotting conflicts before
deploying something new.

Usage:
  portmap.py --all
  portmap.py --host web-01 db-01
  portmap.py --all --port 443          # filter to a specific port
  portmap.py --all --json
"""

import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)

_COLLECT = r"""
# System listening ports via ss
ss -tlnpH 2>/dev/null | awk '{
    split($4, a, ":")
    port = a[length(a)]
    proc = $6
    gsub(/users:\(\("/, "", proc); gsub(/".*/, "", proc)
    if (port+0 > 0) printf "SYS %s %s\n", port, proc
}'
# Docker host-mapped ports
if command -v docker >/dev/null 2>&1; then
    docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | while read -r name ports; do
        echo "$ports" | tr ',' '\n' | while read -r mapping; do
            # matches patterns like 0.0.0.0:8080->80/tcp
            host_port=$(echo "$mapping" | grep -oE ':[0-9]+->|^[0-9]+->|0\.0\.0\.0:[0-9]+' | grep -oE '[0-9]+' | head -1)
            [ -n "$host_port" ] && printf "DOCKER %s %s\n" "$host_port" "$name"
        done
    done
fi
"""


def collect(host: dict, timeout: int, verbose: bool) -> dict:
    stdout, err = ssh_run(host, _COLLECT, timeout, verbose)
    if err:
        return {'host': host['name'], 'error': err, 'ports': []}

    ports = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        kind, port_s, name = parts
        try:
            port = int(port_s)
        except ValueError:
            continue
        ports.append({'port': port, 'kind': kind.lower(), 'name': name})

    ports.sort(key=lambda p: p['port'])
    return {'host': host['name'], 'error': None, 'ports': ports}


def print_table(results: list[dict], port_filter: int | None) -> None:
    total = 0
    print()
    print(Colors.bold(fmt('HOST', 22) + fmt('PORT', 8) + fmt('TYPE', 9) + 'PROCESS / CONTAINER'))
    hr(70)
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 22) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue
        rows = [p for p in r['ports'] if (port_filter is None or p['port'] == port_filter)]
        if not rows:
            msg = f"(port {port_filter} not found)" if port_filter else "(no ports)"
            print(fmt(r['host'], 22) + Colors.warn(msg))
            continue
        for i, p in enumerate(rows):
            host_col = r['host'] if i == 0 else ''
            kind_col = Colors.info('docker') if p['kind'] == 'docker' else Colors.ok('system')
            print(fmt(host_col, 22) + fmt(str(p['port']), 8) + fmt(kind_col, 9) + p['name'])
            total += 1
    hr(70)
    print(f"  {Colors.bold('Total:')} {total} listening port(s) across {len(results)} hosts\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='List all listening ports across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--port', type=int, default=None, metavar='N',
                        help='Filter output to a specific port number')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    log_info(f"Scanning ports on {len(hosts)} host(s)…")
    results = run_parallel(
        lambda h: collect(h, args.timeout, args.verbose),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps(results, indent=2))
    else:
        print_table(results, args.port)


if __name__ == '__main__':
    main()
