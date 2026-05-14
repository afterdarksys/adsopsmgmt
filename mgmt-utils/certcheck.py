#!/usr/bin/env python3
"""
certcheck.py - Fleet-wide TLS certificate expiry checker
After Dark Systems - Ops Utils

Finds all listening HTTPS ports on each host, then probes TLS certs from
this machine and reports days until expiry. Flags certs expiring soon.

Usage:
  certcheck.py --all
  certcheck.py --host web-01 lb-01
  certcheck.py --all --warn-days 30 --crit-days 7
  certcheck.py --all --json
"""

import argparse
import json
import ssl
import socket
import sys
import os
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)

# Ports that typically serve TLS
_TLS_PORTS = {443, 8443, 4443, 9443, 6443, 2376, 5000, 8080}

_COLLECT = r"""
# Get all TCP listening ports
ss -tlnpH 2>/dev/null | awk '{
    split($4, a, ":")
    port = a[length(a)]
    if (port+0 > 0) print port
}' | sort -un
"""


def _probe_cert(host_ip: str, port: int, timeout: int) -> dict:
    """Connect and grab the TLS cert. Returns cert info dict or error."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection((host_ip, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host_ip) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    # getpeercert() returns {} for CERT_NONE if no cert presented
                    # Try with binary form
                    der = ssock.getpeercert(binary_form=True)
                    if not der:
                        return {'error': 'no cert'}
                    return {'error': 'could not parse cert'}

                # Parse expiry
                not_after = cert.get('notAfter', '')
                try:
                    expiry = datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z')
                    expiry = expiry.replace(tzinfo=timezone.utc)
                    days_left = (expiry - datetime.now(timezone.utc)).days
                except ValueError:
                    days_left = -1
                    expiry = None

                # Subject CN or SAN
                subject = dict(x[0] for x in cert.get('subject', []))
                cn = subject.get('commonName', '')
                sans = [v for t, v in cert.get('subjectAltName', []) if t == 'DNS']
                name = cn or (sans[0] if sans else 'unknown')

                return {
                    'cn': name,
                    'sans': sans[:3],
                    'expiry': expiry.strftime('%Y-%m-%d') if expiry else not_after,
                    'days_left': days_left,
                    'issuer': dict(x[0] for x in cert.get('issuer', [])).get('organizationName', ''),
                }
    except (ssl.SSLError, ConnectionRefusedError, OSError):
        return {'error': 'no TLS'}
    except Exception as exc:
        return {'error': str(exc)[:60]}


def check_host(host: dict, ssh_timeout: int, probe_timeout: int,
               verbose: bool, extra_ports: list[int]) -> dict:
    stdout, err = ssh_run(host, _COLLECT, ssh_timeout, verbose)
    if err:
        return {'host': host['name'], 'error': err, 'certs': []}

    listening = set()
    for line in stdout.splitlines():
        line = line.strip()
        try:
            listening.add(int(line))
        except ValueError:
            pass

    # Probe known TLS ports that are actually listening, plus any extras
    target_ports = (listening & _TLS_PORTS) | set(extra_ports)

    certs = []
    for port in sorted(target_ports):
        result = _probe_cert(host['hostname'], port, probe_timeout)
        if 'error' not in result or result['error'] != 'no TLS':
            certs.append({'port': port, **result})

    return {'host': host['name'], 'error': None, 'certs': certs}


def print_table(results: list[dict], warn_days: int, crit_days: int) -> int:
    failures = 0
    print()
    print(Colors.bold(fmt('HOST', 20) + fmt('PORT', 7) + fmt('CN / SAN', 36) +
                      fmt('EXPIRY', 13) + fmt('DAYS', 7) + 'STATUS'))
    hr(100)
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 20) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue
        if not r['certs']:
            print(fmt(r['host'], 20) + Colors.warn('(no TLS ports found)'))
            continue
        for i, c in enumerate(r['certs']):
            host_col = r['host'] if i == 0 else ''
            if 'error' in c:
                print(fmt(host_col, 20) + fmt(str(c['port']), 7) +
                      Colors.warn(f"[{c['error']}]"))
                continue
            days = c['days_left']
            if days <= crit_days:
                days_col   = Colors.fail(f"{days}d")
                status_col = Colors.fail('CRITICAL')
                failures  += 1
            elif days <= warn_days:
                days_col   = Colors.warn(f"{days}d")
                status_col = Colors.warn('expiring soon')
                failures  += 1
            else:
                days_col   = Colors.ok(f"{days}d")
                status_col = Colors.ok('ok')

            cn_col = c['cn']
            if len(cn_col) > 34:
                cn_col = cn_col[:33] + '…'
            print(fmt(host_col, 20) + fmt(str(c['port']), 7) +
                  fmt(cn_col, 36) + fmt(c['expiry'], 13) + fmt(days_col, 7) + status_col)
    hr(100)
    print(f"  {Colors.bold('Summary:')} {Colors.fail(str(failures))} cert(s) need attention\n")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Check TLS certificate expiry across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--warn-days', type=int, default=30,
                        help='Warn when cert expires within N days (default: 30)')
    parser.add_argument('--crit-days', type=int, default=7,
                        help='Critical when cert expires within N days (default: 7)')
    parser.add_argument('--port', type=int, action='append', default=[],
                        dest='extra_ports', metavar='N',
                        help='Additional port(s) to probe (can repeat)')
    parser.add_argument('--probe-timeout', type=int, default=5,
                        help='TLS probe timeout in seconds (default: 5)')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    log_info(f"Checking TLS certs on {len(hosts)} host(s)…")
    results = run_parallel(
        lambda h: check_host(h, args.timeout, args.probe_timeout,
                             args.verbose, args.extra_ports),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps(results, indent=2))
    else:
        failures = print_table(results, args.warn_days, args.crit_days)
        if failures:
            sys.exit(1)


if __name__ == '__main__':
    main()
