#!/usr/bin/env python3
"""
patchstatus.py - Fleet-wide OS patch status
After Dark Systems - Ops Utils

Shows pending OS package updates across all hosts, broken down by
security vs regular updates. Supports Rocky Linux (dnf) and Debian/Ubuntu (apt).

Usage:
  patchstatus.py --all
  patchstatus.py --host web-01 db-01
  patchstatus.py --all --security-only
  patchstatus.py --all --json
"""

import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)

_COLLECT = r"""
OS_ID="unknown"
[ -f /etc/os-release ] && . /etc/os-release && OS_ID="${ID:-unknown}"
printf "OS_ID=%s\n" "$OS_ID"
printf "OS_VERSION=%s\n" "${VERSION_ID:-}"

case "$OS_ID" in
  rhel|centos|rocky|almalinux|fedora|ol)
    # dnf exits 100 when updates are available, 0 when up-to-date
    SEC=$(dnf updateinfo list security 2>/dev/null | grep -c 'Important\|Critical\|Moderate\|Low' || echo 0)
    ALL=$(dnf check-update --quiet 2>/dev/null | grep -vc '^$\|^Last\|^Loaded\|^Loading' || echo 0)
    printf "SEC_UPDATES=%s\n" "$SEC"
    printf "ALL_UPDATES=%s\n" "$ALL"
    ;;
  debian|ubuntu|linuxmint)
    apt-get update -qq 2>/dev/null || true
    SEC=$(apt-get -s -o APT::Get::Show-Upgraded=false upgrade 2>/dev/null \
          | grep '^Inst' | grep -c -i 'security' || echo 0)
    ALL=$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst' || echo 0)
    printf "SEC_UPDATES=%s\n" "$SEC"
    printf "ALL_UPDATES=%s\n" "$ALL"
    ;;
  *)
    printf "SEC_UPDATES=unknown\n"
    printf "ALL_UPDATES=unknown\n"
    ;;
esac
"""


def collect(host: dict, timeout: int, verbose: bool) -> dict:
    # apt-get update can be slow; give it extra time
    stdout, err = ssh_run(host, _COLLECT, max(timeout, 60), verbose)
    if err:
        return {'host': host['name'], 'error': err}

    result: dict = {'host': host['name'], 'error': None,
                    'os_id': '', 'os_version': '',
                    'sec_updates': 0, 'all_updates': 0}
    for line in stdout.splitlines():
        k, _, v = line.strip().partition('=')
        k = k.upper()
        if k == 'OS_ID':       result['os_id'] = v
        elif k == 'OS_VERSION': result['os_version'] = v
        elif k == 'SEC_UPDATES':
            result['sec_updates'] = v if v == 'unknown' else int(v or 0)
        elif k == 'ALL_UPDATES':
            result['all_updates'] = v if v == 'unknown' else int(v or 0)
    return result


def print_table(results: list[dict], security_only: bool) -> None:
    needs_patches = sum(
        1 for r in results
        if not r.get('error') and isinstance(r['sec_updates'], int) and r['sec_updates'] > 0
    )
    print()
    print(Colors.bold(fmt('HOST', 24) + fmt('OS', 22) + fmt('SECURITY', 12) +
                      fmt('ALL', 8) + 'STATUS'))
    hr(80)
    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 24) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue

        os_str = f"{r['os_id']} {r['os_version']}".strip()
        sec = r['sec_updates']
        total = r['all_updates']

        if sec == 'unknown':
            sec_col   = Colors.warn('?')
            total_col = Colors.warn('?')
            status    = Colors.warn('unsupported OS')
        else:
            sec_col   = Colors.fail(str(sec)) if sec > 0 else Colors.ok('0')
            total_col = Colors.warn(str(total)) if total > 0 else Colors.ok('0')
            if sec > 0:
                status = Colors.fail('SECURITY UPDATES NEEDED')
            elif total > 0:
                status = Colors.warn('updates available')
            else:
                status = Colors.ok('up to date')

        if security_only and sec == 0:
            continue

        print(fmt(r['host'], 24) + fmt(os_str, 22) +
              fmt(sec_col, 12) + fmt(total_col, 8) + status)

    hr(80)
    print(f"  {Colors.bold('Summary:')} {Colors.fail(str(needs_patches))} host(s) need security patches "
          f"/ {len(results)} checked\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Show OS patch/update status across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--security-only', action='store_true',
                        help='Only show hosts with pending security updates')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    log_info(f"Checking patch status on {len(hosts)} host(s) (may take a moment)…")
    results = run_parallel(
        lambda h: collect(h, args.timeout, args.verbose),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps(results, indent=2))
    else:
        print_table(results, args.security_only)


if __name__ == '__main__':
    main()
