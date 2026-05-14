#!/usr/bin/env python3
"""
healthcheck.py - Fleet-wide service health checker
After Dark Systems - Ops Utils

Discovers running containers on each host, then probes their health endpoints
(/health, /healthz, /) from this machine. Flags anything non-2xx or timing out.

Usage:
  healthcheck.py --all
  healthcheck.py --host web-01 app-01
  healthcheck.py --all --path /healthz        # custom health path
  healthcheck.py --all --warn-only            # exit 0 even when unhealthy
  healthcheck.py --all --json
"""

import argparse
import json
import sys
import os
import socket
import urllib.request
import urllib.error
import time
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)

_COLLECT = r"""
docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null
"""


def _get_containers(host: dict, timeout: int, verbose: bool) -> list[dict]:
    """Return list of {name, host_ip, port} for all containers with mapped ports."""
    stdout, err = ssh_run(host, _COLLECT, timeout, verbose)
    if err:
        return []

    containers = []
    host_ip = host['hostname']

    for line in stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        name, ports_str = parts
        # Parse port mappings like '0.0.0.0:8080->80/tcp, 0.0.0.0:8443->443/tcp'
        for mapping in ports_str.split(','):
            mapping = mapping.strip()
            # Match 0.0.0.0:PORT->... or :::PORT->...
            import re
            m = re.search(r'(?:0\.0\.0\.0|:::?):(\d+)->', mapping)
            if m:
                containers.append({'name': name, 'ip': host_ip, 'port': int(m.group(1))})

    return containers


def _http_probe(ip: str, port: int, paths: list[str], timeout: int) -> tuple[int, str, float]:
    """Try each path in order. Return (status_code, path_used, latency_ms)."""
    for path in paths:
        # Try HTTPS first on 443/8443, HTTP otherwise
        schemes = ['https', 'http'] if port in (443, 8443) else ['http', 'https']
        for scheme in schemes:
            url = f"{scheme}://{ip}:{port}{path}"
            try:
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                t0 = time.monotonic()
                req = urllib.request.Request(url, headers={'User-Agent': 'mgmt-utils/healthcheck'})
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    latency = (time.monotonic() - t0) * 1000
                    return resp.status, path, latency
            except urllib.error.HTTPError as e:
                latency = (time.monotonic() - t0) * 1000
                if e.code < 500:
                    return e.code, path, latency
            except Exception:
                pass
    return 0, paths[0], 0.0


def check_host(host: dict, paths: list[str], probe_timeout: int,
               ssh_timeout: int, verbose: bool) -> dict:
    containers = _get_containers(host, ssh_timeout, verbose)
    if not containers:
        # Check if SSH failed or just no containers
        _, err = ssh_run(host, 'echo ok', ssh_timeout, False)
        if err:
            return {'host': host['name'], 'error': err, 'probes': []}
        return {'host': host['name'], 'error': None, 'probes': []}

    probes = []
    for c in containers:
        status, path, latency = _http_probe(c['ip'], c['port'], paths, probe_timeout)
        probes.append({
            'container': c['name'],
            'url':       f"{c['ip']}:{c['port']}{path}",
            'status':    status,
            'latency_ms': round(latency, 1),
            'ok':        200 <= status < 300,
        })

    return {'host': host['name'], 'error': None, 'probes': probes}


def print_table(results: list[dict]) -> None:
    total = ok = 0
    print()
    print(Colors.bold(fmt('HOST', 20) + fmt('CONTAINER', 28) + fmt('ENDPOINT', 32) +
                      fmt('STATUS', 10) + 'LATENCY'))
    hr(100)
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 20) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue
        if not r['probes']:
            print(fmt(r['host'], 20) + Colors.warn('(no mapped ports)'))
            continue
        for i, p in enumerate(r['probes']):
            host_col   = r['host'] if i == 0 else ''
            status_str = str(p['status']) if p['status'] else 'timeout'
            status_col = Colors.ok(status_str) if p['ok'] else Colors.fail(status_str)
            lat_col    = f"{p['latency_ms']}ms" if p['latency_ms'] else ''
            print(fmt(host_col, 20) + fmt(p['container'], 28) +
                  fmt(p['url'], 32) + fmt(status_col, 10) + lat_col)
            total += 1
            ok += int(p['ok'])
    hr(100)
    failed = total - ok
    summary_color = Colors.fail if failed else Colors.ok
    print(f"  {Colors.bold('Summary:')} {Colors.ok(str(ok))} healthy  "
          f"{summary_color(str(failed))} unhealthy  / {total} endpoints checked\n")
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description='HTTP health-probe all container endpoints across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--path', default=None, metavar='PATH',
                        help='Health endpoint path (default: tries /health, /healthz, /)')
    parser.add_argument('--probe-timeout', type=int, default=5,
                        help='HTTP probe timeout in seconds (default: 5)')
    parser.add_argument('--warn-only', action='store_true',
                        help='Exit 0 even when unhealthy endpoints are found')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    paths = [args.path] if args.path else ['/health', '/healthz', '/']
    log_info(f"Probing health on {len(hosts)} host(s) via {paths}…")

    results = run_parallel(
        lambda h: check_host(h, paths, args.probe_timeout, args.timeout, args.verbose),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps(results, indent=2))
        failed = sum(1 for r in results for p in r.get('probes', []) if not p['ok'])
    else:
        failed = print_table(results) or 0

    if failed and not args.warn_only:
        sys.exit(1)


if __name__ == '__main__':
    main()
