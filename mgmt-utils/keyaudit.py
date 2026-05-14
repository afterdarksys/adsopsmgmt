#!/usr/bin/env python3
"""
keyaudit.py - Fleet-wide SSH authorized_keys audit
After Dark Systems - Ops Utils

Collects all authorized_keys from each host and reports every key present.
Compare against a --baseline file to flag unknown keys.

Usage:
  keyaudit.py --all
  keyaudit.py --all --baseline ~/.ssh/known_fleet_keys.txt
  keyaudit.py --all --users root opc ubuntu admin
  keyaudit.py --all --json
"""

import argparse
import hashlib
import json
import subprocess
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_warn, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)


def _collect_script(users: list[str]) -> str:
    user_list = ' '.join(users)
    return f"""
for user in {user_list}; do
    # Try to get home dir
    home=$(getent passwd "$user" 2>/dev/null | cut -d: -f6)
    [ -z "$home" ] && continue
    keyfile="$home/.ssh/authorized_keys"
    [ -f "$keyfile" ] || continue
    while IFS= read -r line; do
        line=$(echo "$line" | sed 's/^[[:space:]]*//')
        [ -z "$line" ] && continue
        [[ "$line" == \\#* ]] && continue
        printf "KEY %s %s\\n" "$user" "$line"
    done < "$keyfile"
done
"""


def _fingerprint(pubkey_line: str) -> str:
    """Return a short fingerprint (first 16 chars of SHA256 hex) for a key line."""
    # Key lines: [options] keytype base64 [comment]
    parts = pubkey_line.split()
    for i, p in enumerate(parts):
        if p.startswith('ssh-') or p.startswith('ecdsa-') or p.startswith('sk-'):
            if i + 1 < len(parts):
                try:
                    import base64
                    raw = base64.b64decode(parts[i + 1])
                    return hashlib.sha256(raw).hexdigest()[:16]
                except Exception:
                    pass
    return hashlib.sha256(pubkey_line.encode()).hexdigest()[:16]


def collect(host: dict, users: list[str], timeout: int, verbose: bool) -> dict:
    script = _collect_script(users)
    stdout, err = ssh_run(host, script, timeout, verbose)
    if err:
        return {'host': host['name'], 'error': err, 'keys': []}

    keys = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith('KEY '):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        _, user, key_line = parts
        keys.append({
            'user':        user,
            'key':         key_line,
            'fingerprint': _fingerprint(key_line),
            'comment':     key_line.split()[-1] if len(key_line.split()) >= 3 else '',
            'keytype':     next((p for p in key_line.split()
                                 if p.startswith('ssh-') or p.startswith('ecdsa-')), ''),
        })

    return {'host': host['name'], 'error': None, 'keys': keys}


def load_baseline(path: str) -> set[str]:
    """Load a baseline file; returns set of fingerprints."""
    fps = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    fps.add(_fingerprint(line))
    except FileNotFoundError:
        log_error(f"Baseline file not found: {path}")
        sys.exit(1)
    return fps


def print_table(results: list[dict], baseline: set[str]) -> int:
    unknown_count = 0
    all_fps: dict[str, list[str]] = {}  # fingerprint → hosts

    print()
    print(Colors.bold(fmt('HOST', 20) + fmt('USER', 10) + fmt('TYPE', 16) +
                      fmt('FINGERPRINT', 18) + fmt('COMMENT', 28) + 'FLAG'))
    hr(100)

    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 20) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue
        if not r['keys']:
            print(fmt(r['host'], 20) + Colors.ok('(no keys found)'))
            continue
        for i, k in enumerate(r['keys']):
            host_col = r['host'] if i == 0 else ''
            fp       = k['fingerprint']
            all_fps.setdefault(fp, []).append(r['host'])

            unknown = baseline and fp not in baseline
            flag    = Colors.fail('UNKNOWN') if unknown else ''
            if unknown:
                unknown_count += 1

            comment = k['comment'][:26] if k['comment'] != k['keytype'] else ''
            print(fmt(host_col, 20) + fmt(k['user'], 10) + fmt(k['keytype'], 16) +
                  fmt(fp, 18) + fmt(comment, 28) + flag)

    hr(100)

    # Keys seen on multiple hosts
    shared = {fp: hosts for fp, hosts in all_fps.items() if len(hosts) > 1}
    if shared:
        print(f"\n  {Colors.bold('Keys shared across multiple hosts:')}")
        for fp, hosts in sorted(shared.items()):
            print(f"    {fp}  →  {', '.join(sorted(hosts))}")

    if baseline:
        print(f"\n  {Colors.bold('Baseline check:')} "
              f"{Colors.fail(str(unknown_count))} unknown key(s) found")
    print()
    return unknown_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Audit SSH authorized_keys across the fleet.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--users', nargs='+',
                        default=['root', 'opc', 'ubuntu', 'admin', 'ec2-user', 'rocky'],
                        metavar='USER',
                        help='Users to check (default: root opc ubuntu admin ec2-user rocky)')
    parser.add_argument('--baseline', default=None, metavar='FILE',
                        help='File of known-good public keys to compare against')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    baseline = load_baseline(args.baseline) if args.baseline else set()
    log_info(f"Auditing authorized_keys on {len(hosts)} host(s) for users: {args.users}…")

    results = run_parallel(
        lambda h: collect(h, args.users, args.timeout, args.verbose),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps(results, indent=2))
    else:
        unknown = print_table(results, baseline)
        if unknown:
            sys.exit(1)


if __name__ == '__main__':
    main()
