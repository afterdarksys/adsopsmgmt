"""
_lib.py - Shared utilities for mgmt-utils tools.
After Dark Systems - Ops Utils

Provides: Colors, logging, host resolution (inventory DB + SSH config fallback),
          SSH runner, ANSI-aware table formatter, common argparse flags,
          parallel executor.
"""

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

class Colors:
    RED    = '\033[0;31m'
    GREEN  = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE   = '\033[0;34m'
    CYAN   = '\033[0;36m'
    BOLD   = '\033[1m'
    NC     = '\033[0m'

    @staticmethod
    def ok(s: str)   -> str: return f"{Colors.GREEN}{s}{Colors.NC}"
    @staticmethod
    def fail(s: str) -> str: return f"{Colors.RED}{s}{Colors.NC}"
    @staticmethod
    def warn(s: str) -> str: return f"{Colors.YELLOW}{s}{Colors.NC}"
    @staticmethod
    def info(s: str) -> str: return f"{Colors.CYAN}{s}{Colors.NC}"
    @staticmethod
    def bold(s: str) -> str: return f"{Colors.BOLD}{s}{Colors.NC}"
    @staticmethod
    def strip(s: str) -> str: return re.sub(r'\033\[[0-9;]*m', '', s)


def log_info(msg: str)  -> None: print(f"{Colors.BLUE}[INFO]{Colors.NC}  {msg}")
def log_ok(msg: str)    -> None: print(f"{Colors.GREEN}[ OK ]{Colors.NC}  {msg}")
def log_warn(msg: str)  -> None: print(f"{Colors.YELLOW}[WARN]{Colors.NC}  {msg}")
def log_error(msg: str) -> None: print(f"{Colors.RED}[ERR ]{Colors.NC}  {msg}", file=sys.stderr)
def log_step(msg: str)  -> None: print(f"  {Colors.CYAN}→{Colors.NC}  {msg}")


def fmt(s: str, width: int) -> str:
    """Left-align s in a column of visible width, ANSI escape-aware."""
    visible = len(Colors.strip(s))
    return s + ' ' * max(0, width - visible)


def hr(width: int = 110) -> None:
    print('─' * width)


# ---------------------------------------------------------------------------
# Host resolution
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
            kw, val = parts[0].lower(), parts[1].strip()
            if kw == 'host':
                if cur and '*' not in cur['name']:
                    hosts.append(cur)
                cur = (
                    {'name': val, 'hostname': val, 'user': '', 'port': '22', 'key': '',
                     'environment': '', 'region': ''}
                    if '*' not in val else None
                )
            elif cur:
                if   kw == 'hostname':     cur['hostname'] = val
                elif kw == 'user':         cur['user'] = val
                elif kw == 'port':         cur['port'] = val
                elif kw == 'identityfile': cur['key'] = (
                    str(Path.home() / val[2:]) if val.startswith('~/') else val
                )
    if cur and '*' not in cur['name']:
        hosts.append(cur)
    return hosts


def _inventory_hosts(dc_filter: Optional[str]) -> list[dict]:
    """Fetch active hosts from the inventory DB (same env vars as infractl)."""
    user = os.environ.get('INVENTORY_DB_USER', '')
    pw   = os.environ.get('INVENTORY_DB_PASSWORD', '')
    if not user or not pw:
        return []
    try:
        import psycopg2  # type: ignore
    except ImportError:
        log_warn("psycopg2 not installed — pip install psycopg2-binary to use inventory DB")
        return []
    try:
        conn = psycopg2.connect(
            host=os.environ.get('INVENTORY_DB_HOST', 'afterdarksys.com'),
            port=os.environ.get('INVENTORY_DB_PORT', '5432'),
            dbname=os.environ.get('INVENTORY_DB_NAME', 'inventory'),
            user=user, password=pw, sslmode='require', connect_timeout=5,
        )
        cur = conn.cursor()
        q = """
            SELECT resource_name, hostname,
                   metadata->>'ip',       metadata->>'ssh_user',
                   metadata->>'ssh_port', metadata->>'ssh_key',
                   environment, region
            FROM resources
            WHERE status = 'active'
        """
        params: list = []
        if dc_filter:
            q += " AND (environment = %s OR region = %s)"
            params.extend([dc_filter, dc_filter])
        cur.execute(q, params)
        rows = cur.fetchall()
        conn.close()
        return [
            {
                'name': name or host, 'hostname': ip or host,
                'user': u or '',      'port': p or '22',
                'key':  k or '',      'environment': env or '',
                'region': reg or '',
            }
            for name, host, ip, u, p, k, env, reg in rows
        ]
    except Exception as exc:
        log_warn(f"Inventory DB unavailable ({exc}) — falling back to SSH config")
        return []


def resolve_hosts(
    names: Optional[list[str]],
    all_hosts: bool,
    dc_filter: Optional[str] = None,
) -> list[dict]:
    """Return the list of host dicts to operate on."""
    if names:
        by_name = {h['name']: h for h in _ssh_config_hosts()}
        return [
            by_name.get(n, {'name': n, 'hostname': n, 'user': '', 'port': '22', 'key': '',
                            'environment': '', 'region': ''})
            for n in names
        ]
    if all_hosts:
        inv = _inventory_hosts(dc_filter)
        if inv:
            return inv
        ssh = _ssh_config_hosts()
        if dc_filter:
            log_warn(f"Inventory unavailable — SSH config has no DC metadata; ignoring --dc={dc_filter}")
        return ssh
    return []


# ---------------------------------------------------------------------------
# SSH execution
# ---------------------------------------------------------------------------

def ssh_run(
    host: dict,
    script: str,
    timeout: int = 15,
    verbose: bool = False,
) -> tuple[str, Optional[str]]:
    """Run script on host via SSH.

    Returns:
        (stdout, None)         on success
        ('',   error_message)  on failure
    """
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
    args += [target, script]

    if verbose:
        print(f"  [ssh] {host['name']} ({target})", file=sys.stderr)

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
        if proc.returncode != 0:
            err = (proc.stderr.strip() or f"exit {proc.returncode}").splitlines()[0]
            return '', err
        return proc.stdout, None
    except subprocess.TimeoutExpired:
        return '', f'timed out after {timeout}s'
    except Exception as exc:
        return '', str(exc)


# ---------------------------------------------------------------------------
# Argparse helpers
# ---------------------------------------------------------------------------

def add_common_args(parser, require_target: bool = True) -> None:
    """Add standard flags to an argparse parser: --host/--all, --dc, --timeout,
    --concurrency, --json, --verbose."""
    grp = parser.add_mutually_exclusive_group(required=require_target)
    grp.add_argument('--host', metavar='NAME', nargs='+',
                     help='Specific host(s) to target')
    grp.add_argument('--all', dest='all_hosts', action='store_true',
                     help='All active hosts from inventory / SSH config')
    parser.add_argument('--dc', metavar='NAME', default=None,
                        help='Filter to hosts in this datacenter/environment (with --all)')
    parser.add_argument('--timeout', type=int, default=15,
                        help='SSH timeout per host in seconds (default: 15)')
    parser.add_argument('--concurrency', type=int, default=8,
                        help='Max parallel SSH connections (default: 8)')
    parser.add_argument('--json', dest='json_out', action='store_true',
                        help='Emit JSON instead of a table')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Print each SSH command before running it')


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------

def run_parallel(fn: Callable, hosts: list[dict], concurrency: int = 8) -> list:
    """Run fn(host) for each host in parallel; return results in completion order."""
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fn, h): h for h in hosts}
        for fut in as_completed(futures):
            results.append(fut.result())
    return results
