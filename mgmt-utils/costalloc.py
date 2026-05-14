#!/usr/bin/env python3
"""
costalloc.py - Container cost allocation
After Dark Systems - Ops Utils

Joins running Docker containers with the host's daily/monthly cost from the
inventory database, then splits the cost evenly across containers on that host.
Gives a rough per-service cost estimate.

Requires: INVENTORY_DB_USER and INVENTORY_DB_PASSWORD env vars (same as infractl).

Usage:
  costalloc.py --all
  costalloc.py --host web-01 db-01
  costalloc.py --all --dc prod --json
"""

import argparse
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_warn, log_error,
                  add_common_args, resolve_hosts, ssh_run, run_parallel,
                  _inventory_hosts)

_COLLECT = r"""
docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null
"""


def _fetch_costs(hosts: list[dict]) -> dict[str, dict]:
    """Return {hostname: {daily_cost, monthly_cost}} from inventory DB."""
    user = os.environ.get('INVENTORY_DB_USER', '')
    pw   = os.environ.get('INVENTORY_DB_PASSWORD', '')
    if not user or not pw:
        return {}
    try:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(
            host=os.environ.get('INVENTORY_DB_HOST', 'afterdarksys.com'),
            port=os.environ.get('INVENTORY_DB_PORT', '5432'),
            dbname=os.environ.get('INVENTORY_DB_NAME', 'inventory'),
            user=user, password=pw, sslmode='require', connect_timeout=5,
        )
        cur = conn.cursor()
        names = [h['name'] for h in hosts]
        cur.execute(
            "SELECT resource_name, average_daily_cost, average_monthly_cost "
            "FROM resources WHERE resource_name = ANY(%s)",
            (names,)
        )
        costs = {}
        for name, daily, monthly in cur.fetchall():
            costs[name] = {
                'daily':   float(daily)   if daily   else 0.0,
                'monthly': float(monthly) if monthly else 0.0,
            }
        conn.close()
        return costs
    except Exception as exc:
        log_warn(f"Could not fetch costs from inventory DB: {exc}")
        return {}


def collect(host: dict, timeout: int, verbose: bool) -> dict:
    stdout, err = ssh_run(host, _COLLECT, timeout, verbose)
    if err:
        return {'host': host['name'], 'error': err, 'containers': []}

    containers = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            containers.append({'name': parts[0], 'image': parts[1]})

    return {'host': host['name'], 'error': None, 'containers': containers}


def print_table(results: list[dict], costs: dict[str, dict]) -> None:
    grand_daily = grand_monthly = 0.0
    no_cost_hosts = []

    print()
    print(Colors.bold(fmt('HOST', 22) + fmt('CONTAINER', 30) + fmt('IMAGE', 36) +
                      fmt('DAILY $', 10) + 'MONTHLY $'))
    hr(110)

    for r in sorted(results, key=lambda x: x['host']):
        if r['error']:
            print(fmt(r['host'], 22) + Colors.warn(f"UNREACHABLE  {r['error']}"))
            continue

        host_costs = costs.get(r['host'], {})
        daily_total   = host_costs.get('daily', 0.0)
        monthly_total = host_costs.get('monthly', 0.0)
        n = len(r['containers'])

        if daily_total == 0.0 and monthly_total == 0.0:
            no_cost_hosts.append(r['host'])

        if not r['containers']:
            print(fmt(r['host'], 22) + Colors.warn('(no containers)'))
            continue

        share_daily   = daily_total   / n if n else 0.0
        share_monthly = monthly_total / n if n else 0.0

        for i, c in enumerate(r['containers']):
            host_col = r['host'] if i == 0 else ''
            img = c['image']
            if len(img) > 34:
                img = '…' + img[-33:]
            daily_s   = f"${share_daily:.2f}"   if share_daily   else '—'
            monthly_s = f"${share_monthly:.2f}" if share_monthly else '—'
            print(fmt(host_col, 22) + fmt(c['name'], 30) + fmt(img, 36) +
                  fmt(daily_s, 10) + monthly_s)
            grand_daily   += share_daily
            grand_monthly += share_monthly

    hr(110)
    if no_cost_hosts:
        log_warn(f"No cost data for: {', '.join(no_cost_hosts)} "
                 f"(set average_daily_cost / average_monthly_cost in inventory)")
    print(f"\n  {Colors.bold('Estimated fleet run cost:')} "
          f"{Colors.ok(f'${grand_daily:.2f}/day')}  "
          f"{Colors.ok(f'${grand_monthly:.2f}/month')}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Estimate per-container cost from inventory data.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    log_info(f"Collecting containers from {len(hosts)} host(s)…")
    results = run_parallel(
        lambda h: collect(h, args.timeout, args.verbose),
        hosts, args.concurrency)

    costs = _fetch_costs(hosts)
    if not costs:
        log_warn("No cost data available — set INVENTORY_DB_USER/INVENTORY_DB_PASSWORD "
                 "and populate average_daily_cost in the inventory.")

    if args.json_out:
        for r in results:
            r['costs'] = costs.get(r['host'], {})
        print(json.dumps(results, indent=2))
    else:
        print_table(results, costs)


if __name__ == '__main__':
    main()
