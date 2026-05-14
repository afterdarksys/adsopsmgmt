#!/usr/bin/env python3
"""
capacitycheck.py - Host Capacity Checker
After Dark Systems - Ops Utils

Check whether a host (or set of hosts) has sufficient available capacity
to run a new workload described by a system spec.

Spec format:
  --sysspec "cpu=12,ram=96,hd=500g"
  --sysspec "cpu=4,ram=16,hd=200g,dc=prod"

Spec fields:
  cpu=N       Need N logical CPU cores with headroom (total cores minus 1-min load)
  ram=N[g|m]  Need N GB (or MB) of available RAM (MemAvailable from /proc/meminfo)
  hd=N[g|t]   Need N GB (or TB) free on any single mount point
  dc=NAME     Filter to hosts in this datacenter/environment (use ???? for any)

Examples:
  capacitycheck.py --host web-01 --sysspec "cpu=4,ram=8,hd=100g"
  capacitycheck.py --all --sysspec "cpu=8,ram=32,hd=500g,dc=prod"
  capacitycheck.py --all --sysspec "cpu=4,ram=16,hd=200g" --json
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

class Colors:
    RED    = '\033[0;31m'
    GREEN  = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE   = '\033[0;34m'
    BOLD   = '\033[1m'
    NC     = '\033[0m'

    @staticmethod
    def ok(s: str) -> str:    return f"{Colors.GREEN}{s}{Colors.NC}"
    @staticmethod
    def fail(s: str) -> str:  return f"{Colors.RED}{s}{Colors.NC}"
    @staticmethod
    def warn(s: str) -> str:  return f"{Colors.YELLOW}{s}{Colors.NC}"
    @staticmethod
    def bold(s: str) -> str:  return f"{Colors.BOLD}{s}{Colors.NC}"


def log_info(msg: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")

def log_warn(msg: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")

def log_error(msg: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------

def _parse_size_to_gb(value: str) -> float:
    """Parse a human size string into GB. E.g. '96' → 96.0, '500g' → 500.0, '2t' → 2048.0."""
    v = value.strip().lower()
    if v.endswith('t'):
        return float(v[:-1]) * 1024
    if v.endswith('g'):
        return float(v[:-1])
    if v.endswith('m'):
        return float(v[:-1]) / 1024
    # No unit: assume GB
    return float(v)


def parse_sysspec(spec: str) -> dict:
    """Parse --sysspec string into requirement dict.

    Returns:
        {
            'cpu': int | None,
            'ram_gb': float | None,
            'hd_gb': float | None,
            'dc': str | None,
        }
    """
    result: dict = {'cpu': None, 'ram_gb': None, 'hd_gb': None, 'dc': None}
    for part in spec.split(','):
        part = part.strip()
        if '=' not in part:
            continue
        key, _, val = part.partition('=')
        key = key.strip().lower()
        val = val.strip()
        if not val:
            continue
        if key == 'cpu':
            result['cpu'] = int(val)
        elif key == 'ram':
            result['ram_gb'] = _parse_size_to_gb(val)
        elif key == 'hd':
            result['hd_gb'] = _parse_size_to_gb(val)
        elif key == 'dc':
            # '????' means unspecified — treat as no filter
            if val and val != '????':
                result['dc'] = val
    return result


# ---------------------------------------------------------------------------
# Host resolution
# (mirrors infractl's inventory.GetHost / ListHosts with Python ssh-config fallback)
# ---------------------------------------------------------------------------

def _ssh_config_hosts() -> list[dict]:
    """Parse ~/.ssh/config for non-wildcard Host entries."""
    config_path = Path.home() / '.ssh' / 'config'
    if not config_path.exists():
        return []

    hosts: list[dict] = []
    cur: Optional[dict] = None

    with open(config_path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            keyword, value = parts[0].lower(), parts[1].strip()

            if keyword == 'host':
                if cur and '*' not in cur['name']:
                    hosts.append(cur)
                cur = (
                    {'name': value, 'hostname': value, 'user': '', 'port': '22', 'key': ''}
                    if '*' not in value else None
                )
            elif cur:
                if keyword == 'hostname':
                    cur['hostname'] = value
                elif keyword == 'user':
                    cur['user'] = value
                elif keyword == 'port':
                    cur['port'] = value
                elif keyword == 'identityfile':
                    if value.startswith('~/'):
                        value = str(Path.home() / value[2:])
                    cur['key'] = value

    if cur and '*' not in cur['name']:
        hosts.append(cur)

    return hosts


def _inventory_hosts(dc_filter: Optional[str]) -> list[dict]:
    """Fetch hosts from the inventory DB (same env vars as infractl)."""
    db_host = os.environ.get('INVENTORY_DB_HOST', 'afterdarksys.com')
    db_port = os.environ.get('INVENTORY_DB_PORT', '5432')
    db_name = os.environ.get('INVENTORY_DB_NAME', 'inventory')
    db_user = os.environ.get('INVENTORY_DB_USER', '')
    db_pass = os.environ.get('INVENTORY_DB_PASSWORD', '')

    if not db_user or not db_pass:
        return []

    try:
        import psycopg2  # type: ignore
    except ImportError:
        log_warn("psycopg2 not installed — cannot query inventory DB (pip install psycopg2-binary)")
        return []

    try:
        conn = psycopg2.connect(
            host=db_host, port=db_port, dbname=db_name,
            user=db_user, password=db_pass,
            sslmode='require', connect_timeout=5,
        )
        cur = conn.cursor()

        query = """
            SELECT resource_name, hostname,
                   metadata->>'ip', metadata->>'ssh_user',
                   metadata->>'ssh_port', metadata->>'ssh_key',
                   environment, region
            FROM resources
            WHERE status = 'active'
        """
        params: list = []
        if dc_filter:
            query += " AND (environment = %s OR region = %s)"
            params.extend([dc_filter, dc_filter])

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        hosts = []
        for name, hostname, ip, ssh_user, ssh_port, ssh_key, env, region in rows:
            hosts.append({
                'name': name or hostname,
                'hostname': ip or hostname,
                'user': ssh_user or '',
                'port': ssh_port or '22',
                'key': ssh_key or '',
                'environment': env or '',
                'region': region or '',
            })
        return hosts

    except Exception as exc:
        log_warn(f"Inventory DB unavailable ({exc}) — falling back to SSH config")
        return []


def resolve_hosts(
    names: Optional[list[str]],
    all_hosts: bool,
    dc_filter: Optional[str],
) -> list[dict]:
    """Return the list of host dicts to probe."""
    if names:
        by_name = {h['name']: h for h in _ssh_config_hosts()}
        return [by_name.get(n, {'name': n, 'hostname': n, 'user': '', 'port': '22', 'key': ''})
                for n in names]

    if all_hosts:
        inv = _inventory_hosts(dc_filter)
        if inv:
            return inv
        ssh_hosts = _ssh_config_hosts()
        if dc_filter:
            log_warn(f"Inventory unavailable — SSH config has no DC metadata; ignoring dc={dc_filter}")
        return ssh_hosts

    return []


# ---------------------------------------------------------------------------
# Remote collection
# ---------------------------------------------------------------------------

# Shell script run on each host via SSH.
# Outputs KEY=VALUE lines (same pattern as infractl scan).
_COLLECT_SCRIPT = r"""
set -e
CPU_CORES=$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)
printf "CPU_CORES=%s\n" "$CPU_CORES"
LOAD_1=$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)
printf "LOAD_1=%s\n" "$LOAD_1"
MEM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
MEM_AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
printf "MEM_TOTAL_KB=%s\n" "$MEM_TOTAL_KB"
printf "MEM_AVAIL_KB=%s\n" "$MEM_AVAIL_KB"
df -Pk 2>/dev/null \
  | awk 'NR>1 && $1!~/^(tmpfs|devtmpfs|squashfs|udev|none|overlay)/ \
              && $6!~/^(\/dev\/|\/sys\/|\/proc\/|\/run\/)/ {
    printf "DISK %s SIZE_KB=%s AVAIL_KB=%s\n", $6, $2, $4
  }'
"""


def ssh_collect(host: dict, timeout: int, verbose: bool) -> dict:
    """SSH into host and run _COLLECT_SCRIPT. Returns stats dict or {'error': ...}."""
    target = host['hostname']
    args = [
        'ssh',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', f'ConnectTimeout={timeout}',
        '-o', 'BatchMode=yes',
    ]
    if host.get('port') and host['port'] not in ('', '22'):
        args += ['-p', host['port']]
    if host.get('key'):
        args += ['-i', host['key']]
    if host.get('user'):
        target = f"{host['user']}@{target}"
    args.append(target)
    args.append(_COLLECT_SCRIPT)

    if verbose:
        print(f"  [ssh] connecting to {host['name']} ({target})", file=sys.stderr)

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout + 5
        )
        if proc.returncode != 0:
            return {'error': (proc.stderr.strip() or f"ssh exit {proc.returncode}").splitlines()[0]}
        return _parse_stats(proc.stdout)
    except subprocess.TimeoutExpired:
        return {'error': f'SSH timed out after {timeout}s'}
    except Exception as exc:
        return {'error': str(exc)}


def _parse_stats(output: str) -> dict:
    """Parse KEY=VALUE output from _COLLECT_SCRIPT into a stats dict."""
    stats: dict = {
        'cpu_cores': 0,
        'load_1m': 0.0,
        'mem_total_kb': 0,
        'mem_avail_kb': 0,
        'disks': [],
    }
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('DISK '):
            # Format: DISK <mount> SIZE_KB=<n> AVAIL_KB=<n>
            parts = line.split()
            if len(parts) >= 4:
                try:
                    size_kb  = int(parts[2].split('=', 1)[1])
                    avail_kb = int(parts[3].split('=', 1)[1])
                    stats['disks'].append({'mount': parts[1], 'size_kb': size_kb, 'avail_kb': avail_kb})
                except (ValueError, IndexError):
                    pass
        elif '=' in line:
            k, _, v = line.partition('=')
            k = k.strip().upper()
            v = v.strip()
            try:
                if k == 'CPU_CORES':
                    stats['cpu_cores'] = int(v)
                elif k == 'LOAD_1':
                    stats['load_1m'] = float(v)
                elif k == 'MEM_TOTAL_KB':
                    stats['mem_total_kb'] = int(v)
                elif k == 'MEM_AVAIL_KB':
                    stats['mem_avail_kb'] = int(v)
            except ValueError:
                pass
    return stats


# ---------------------------------------------------------------------------
# Capacity evaluation
# ---------------------------------------------------------------------------

def check_capacity(stats: dict, spec: dict) -> dict:
    """Evaluate stats against spec requirements.

    Returns:
        {
            'fits': bool,
            'reasons': [str, ...],   # why it does NOT fit (empty if it fits)
            'details': {...},        # per-resource breakdown
        }
    """
    reasons: list[str] = []
    details: dict = {}

    # -- CPU --
    if spec['cpu'] is not None:
        cores    = stats['cpu_cores']
        load     = stats['load_1m']
        headroom = cores - load            # logical cores not busy under current load
        required = spec['cpu']
        details['cpu'] = {
            'cores': cores,
            'load_1m': round(load, 2),
            'headroom': round(headroom, 2),
            'required': required,
        }
        if cores < required:
            reasons.append(f"CPU: host has {cores} cores, need {required}")
        elif headroom < required:
            reasons.append(
                f"CPU: {cores} cores but only {headroom:.1f} free "
                f"(load={load:.2f}), need {required}"
            )

    # -- RAM --
    if spec['ram_gb'] is not None:
        avail_gb = stats['mem_avail_kb'] / 1_048_576
        total_gb = stats['mem_total_kb']  / 1_048_576
        required = spec['ram_gb']
        details['ram'] = {
            'total_gb':    round(total_gb,  1),
            'avail_gb':    round(avail_gb,  1),
            'required_gb': required,
        }
        if avail_gb < required:
            reasons.append(
                f"RAM: {avail_gb:.1f}GB available (of {total_gb:.1f}GB total), "
                f"need {required:.0f}GB"
            )

    # -- Disk: find the mount with the most free space --
    if spec['hd_gb'] is not None:
        best_mount  = None
        best_avail  = 0.0
        for d in stats.get('disks', []):
            a = d['avail_kb'] / 1_048_576
            if a > best_avail:
                best_avail = a
                best_mount = d['mount']
        required = spec['hd_gb']
        details['disk'] = {
            'best_mount':  best_mount or '/',
            'avail_gb':    round(best_avail, 1),
            'required_gb': required,
            'mounts': [
                {
                    'mount':    d['mount'],
                    'total_gb': round(d['size_kb']  / 1_048_576, 1),
                    'avail_gb': round(d['avail_kb'] / 1_048_576, 1),
                }
                for d in stats.get('disks', [])
            ],
        }
        if best_avail < required:
            reasons.append(
                f"Disk: {best_avail:.1f}GB free on best mount ({best_mount}), "
                f"need {required:.0f}GB"
            )

    return {'fits': not reasons, 'reasons': reasons, 'details': details}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt(s: str, width: int) -> str:
    """Left-pad a string to width, ignoring ANSI escape bytes."""
    import re
    visible = len(re.sub(r'\033\[[0-9;]*m', '', s))
    padding = max(0, width - visible)
    return s + ' ' * padding


def print_table(results: list[dict], spec: dict) -> None:
    spec_parts = []
    if spec['cpu']:     spec_parts.append(f"cpu≥{spec['cpu']}")
    if spec['ram_gb']:  spec_parts.append(f"ram≥{spec['ram_gb']:.0f}GB avail")
    if spec['hd_gb']:   spec_parts.append(f"disk≥{spec['hd_gb']:.0f}GB free")
    if spec['dc']:      spec_parts.append(f"dc={spec['dc']}")
    print(f"\n{Colors.bold('Spec:')} {', '.join(spec_parts) or '(none)'}\n")

    HDR = f"{'HOST':<26} {'STATUS':<10} {'CPU (cores/load)':<20} {'RAM (avail/total)':<22} {'DISK (best mount)':<24} REASONS"
    print(Colors.bold(HDR))
    print('─' * 120)

    for r in results:
        name = r['host']['name']
        err  = r.get('error')

        if err:
            row  = _fmt(Colors.warn(name), 26)
            row += _fmt(Colors.warn('UNREACHABLE'), 10)
            row += Colors.warn(err)
            print(row)
            continue

        stats   = r['stats']
        check   = r['check']
        details = check['details']
        fits    = check['fits']

        # CPU column
        cpu_d = details.get('cpu', {})
        if cpu_d:
            cpu_str = f"{cpu_d['cores']}c / {cpu_d['load_1m']:.2f}"
        else:
            cpu_str = f"{stats['cpu_cores']}c"

        # RAM column
        ram_d = details.get('ram', {})
        if ram_d:
            ram_str = f"{ram_d['avail_gb']:.0f}GB / {ram_d['total_gb']:.0f}GB"
        else:
            avail = stats['mem_avail_kb'] / 1_048_576
            total = stats['mem_total_kb']  / 1_048_576
            ram_str = f"{avail:.0f}GB / {total:.0f}GB"

        # Disk column
        disk_d = details.get('disk', {})
        if disk_d:
            disk_str = f"{disk_d['avail_gb']:.0f}GB ({disk_d['best_mount']})"
        elif stats.get('disks'):
            best = max(stats['disks'], key=lambda d: d['avail_kb'])
            disk_str = f"{best['avail_kb'] / 1_048_576:.0f}GB ({best['mount']})"
        else:
            disk_str = 'n/a'

        status_col = Colors.ok('FITS') if fits else Colors.fail('NO FIT')
        reasons_str = Colors.warn('; '.join(check['reasons'])) if check['reasons'] else ''

        row  = _fmt(name,        26)
        row += _fmt(status_col,  10)
        row += _fmt(cpu_str,     20)
        row += _fmt(ram_str,     22)
        row += _fmt(disk_str,    24)
        row += reasons_str
        print(row)

    print()
    fits_n    = sum(1 for r in results if not r.get('error') and r['check']['fits'])
    nofit_n   = sum(1 for r in results if not r.get('error') and not r['check']['fits'])
    unreach_n = sum(1 for r in results if r.get('error'))
    print(
        f"  {Colors.bold('Summary')}: "
        f"{Colors.ok(str(fits_n))} fit  "
        f"{Colors.fail(str(nofit_n))} cannot fit  "
        f"{Colors.warn(str(unreach_n))} unreachable  "
        f"/ {len(results)} checked"
    )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Check host capacity against a system spec.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--host', metavar='NAME', nargs='+',
                        help='One or more specific hosts to check')
    target.add_argument('--all', dest='all_hosts', action='store_true',
                        help='Check all hosts from inventory / SSH config')

    parser.add_argument('--sysspec', required=True, metavar='SPEC',
                        help='System requirements: cpu=N,ram=Ng,hd=Ng[,dc=NAME]')
    parser.add_argument('--timeout', type=int, default=15,
                        help='SSH timeout per host in seconds (default: 15)')
    parser.add_argument('--concurrency', type=int, default=8,
                        help='Max parallel SSH connections (default: 8)')
    parser.add_argument('--json', dest='json_out', action='store_true',
                        help='Output JSON instead of a table')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Print each SSH command before running it')

    args = parser.parse_args()

    spec  = parse_sysspec(args.sysspec)
    hosts = resolve_hosts(
        names=args.host,
        all_hosts=args.all_hosts,
        dc_filter=spec.get('dc'),
    )

    if not hosts:
        log_error(
            "No hosts found. Use --host NAME or --all with a configured "
            "inventory DB (INVENTORY_DB_USER/INVENTORY_DB_PASSWORD) or ~/.ssh/config."
        )
        sys.exit(1)

    if not args.json_out:
        dc_tag = f" in dc={spec['dc']}" if spec.get('dc') else ''
        log_info(f"Checking {len(hosts)} host(s){dc_tag}  spec: {args.sysspec}")

    # Probe all hosts in parallel
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(ssh_collect, h, args.timeout, args.verbose): h for h in hosts}
        for fut in as_completed(futures):
            host  = futures[fut]
            stats = fut.result()
            if 'error' in stats:
                results.append({'host': host, 'error': stats['error']})
            else:
                results.append({'host': host, 'stats': stats, 'check': check_capacity(stats, spec)})

    # Sort: fits first, then no-fit, then unreachable — alpha within each group
    results.sort(key=lambda r: (
        2 if r.get('error') else (0 if r['check']['fits'] else 1),
        r['host']['name'],
    ))

    if args.json_out:
        out = []
        for r in results:
            entry: dict = {'host': r['host']['name']}
            if r.get('error'):
                entry['error'] = r['error']
            else:
                entry['fits']    = r['check']['fits']
                entry['reasons'] = r['check']['reasons']
                entry['details'] = r['check']['details']
            out.append(entry)
        print(json.dumps(out, indent=2))
    else:
        print_table(results, spec)

    # Exit 0 if at least one host fits, 1 otherwise
    any_fits = any(not r.get('error') and r['check']['fits'] for r in results)
    sys.exit(0 if any_fits else 1)


if __name__ == '__main__':
    main()
