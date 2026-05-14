#!/usr/bin/env python3
"""
drainhost.py - Gracefully drain a host for maintenance
After Dark Systems - Ops Utils

Stops all running Docker containers and/or cordons+drains the k3s node,
then optionally marks the host as 'maintenance' in the inventory.

Usage:
  drainhost.py --host web-01
  drainhost.py --host web-01 --k3s              # also cordon/drain k3s node
  drainhost.py --host web-01 --force            # no confirmation prompt
  drainhost.py --host web-01 --update-inventory # mark maintenance in hostctl DB
  drainhost.py --host web-01 --undrain          # restore: start containers + uncordon
"""

import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, log_info, log_ok, log_warn, log_error, log_step,
                  resolve_hosts, ssh_run)

_LIST_SCRIPT = r"""
echo "=== DOCKER ==="
docker ps --format '{{.Names}}  {{.Image}}  {{.Status}}' 2>/dev/null || echo "(docker not available)"
echo "=== K3S ==="
k3s kubectl get nodes --no-headers 2>/dev/null | awk '{print $1, $2}' || echo "(k3s not available)"
"""

_DRAIN_DOCKER = r"""
CONTAINERS=$(docker ps -q 2>/dev/null)
if [ -z "$CONTAINERS" ]; then
    echo "NO_CONTAINERS"
else
    echo "$CONTAINERS" | xargs docker stop --time 30 2>&1
    echo "DOCKER_DRAINED"
fi
"""

_DRAIN_K3S = r"""
NODE=$(k3s kubectl get nodes --no-headers 2>/dev/null | awk 'NR==1{print $1}')
if [ -z "$NODE" ]; then
    echo "K3S_NOT_FOUND"
else
    echo "DRAINING_NODE=$NODE"
    k3s kubectl cordon "$NODE" 2>&1
    k3s kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=120s 2>&1
    echo "K3S_DRAINED=$NODE"
fi
"""

_UNDRAIN_DOCKER = r"""
STOPPED=$(docker ps -a --filter status=exited --format '{{.Names}}' 2>/dev/null)
if [ -z "$STOPPED" ]; then
    echo "NO_STOPPED_CONTAINERS"
else
    echo "$STOPPED" | xargs -I{} docker start {} 2>&1
    echo "DOCKER_RESTORED"
fi
"""

_UNDRAIN_K3S = r"""
NODE=$(k3s kubectl get nodes --no-headers 2>/dev/null | awk 'NR==1{print $1}')
if [ -n "$NODE" ]; then
    k3s kubectl uncordon "$NODE" 2>&1
    echo "K3S_UNCORDONED=$NODE"
fi
"""


def _update_inventory_status(hostname: str, status: str) -> bool:
    user = os.environ.get('INVENTORY_DB_USER', '')
    pw   = os.environ.get('INVENTORY_DB_PASSWORD', '')
    if not user or not pw:
        log_warn("INVENTORY_DB_USER/INVENTORY_DB_PASSWORD not set — skipping inventory update")
        return False
    try:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(
            host=os.environ.get('INVENTORY_DB_HOST', 'afterdarksys.com'),
            port=os.environ.get('INVENTORY_DB_PORT', '5432'),
            dbname=os.environ.get('INVENTORY_DB_NAME', 'inventory'),
            user=user, password=pw, sslmode='require', connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("UPDATE resources SET status = %s WHERE resource_name = %s",
                    (status, hostname))
        conn.commit()
        conn.close()
        return cur.rowcount > 0
    except Exception as exc:
        log_warn(f"Inventory update failed: {exc}")
        return False


def show_status(host: dict, timeout: int, verbose: bool) -> None:
    stdout, err = ssh_run(host, _LIST_SCRIPT, timeout, verbose)
    if err:
        log_error(f"Cannot reach {host['name']}: {err}")
        return
    print(stdout)


def drain(host: dict, do_k3s: bool, update_inv: bool,
          timeout: int, verbose: bool) -> bool:
    hostname = host['name']
    success = True

    # Docker drain
    log_step(f"Stopping Docker containers on {hostname}…")
    stdout, err = ssh_run(host, _DRAIN_DOCKER, max(timeout, 60), verbose)
    if err:
        log_error(f"Docker drain failed: {err}")
        success = False
    elif 'NO_CONTAINERS' in stdout:
        log_warn(f"No running containers on {hostname}")
    elif 'DOCKER_DRAINED' in stdout:
        log_ok(f"Docker containers stopped on {hostname}")
    else:
        log_warn(f"Docker drain output: {stdout.strip()[:200]}")

    # k3s drain
    if do_k3s:
        log_step(f"Cordoning and draining k3s node on {hostname}…")
        stdout, err = ssh_run(host, _DRAIN_K3S, max(timeout, 180), verbose)
        if err:
            log_error(f"k3s drain failed: {err}")
            success = False
        elif 'K3S_NOT_FOUND' in stdout:
            log_warn("k3s not found on this host")
        elif 'K3S_DRAINED' in stdout:
            node = next((l.split('=')[1] for l in stdout.splitlines()
                         if l.startswith('K3S_DRAINED=')), hostname)
            log_ok(f"k3s node {node} cordoned and drained")
        else:
            log_warn(f"k3s drain output: {stdout.strip()[:200]}")

    # Inventory update
    if update_inv and success:
        log_step(f"Marking {hostname} as 'maintenance' in inventory…")
        if _update_inventory_status(hostname, 'maintenance'):
            log_ok(f"{hostname} status → maintenance")
        else:
            log_warn("Inventory update had no effect (host not found?)")

    return success


def undrain(host: dict, do_k3s: bool, update_inv: bool,
            timeout: int, verbose: bool) -> bool:
    hostname = host['name']

    log_step(f"Restarting stopped containers on {hostname}…")
    stdout, err = ssh_run(host, _UNDRAIN_DOCKER, timeout, verbose)
    if err:
        log_error(f"Docker restore failed: {err}")
        return False
    if 'DOCKER_RESTORED' in stdout:
        log_ok(f"Containers restarted on {hostname}")
    else:
        log_warn("No stopped containers found to restart")

    if do_k3s:
        log_step(f"Uncordoning k3s node on {hostname}…")
        stdout, err = ssh_run(host, _UNDRAIN_K3S, timeout, verbose)
        if err:
            log_error(f"k3s uncordon failed: {err}")
        elif 'K3S_UNCORDONED' in stdout:
            node = stdout.split('=')[-1].strip()
            log_ok(f"k3s node {node} uncordoned")

    if update_inv:
        log_step(f"Marking {hostname} as 'active' in inventory…")
        if _update_inventory_status(hostname, 'active'):
            log_ok(f"{hostname} status → active")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Gracefully drain a host for maintenance.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)

    parser.add_argument('--host', required=True, metavar='NAME',
                        help='Host to drain')
    parser.add_argument('--k3s', action='store_true',
                        help='Also cordon and drain the k3s node')
    parser.add_argument('--force', action='store_true',
                        help='Skip confirmation prompt')
    parser.add_argument('--update-inventory', action='store_true',
                        help="Set host status to 'maintenance' (or 'active' on undrain) in DB")
    parser.add_argument('--undrain', action='store_true',
                        help='Reverse: restart containers and uncordon k3s node')
    parser.add_argument('--timeout', type=int, default=30,
                        help='SSH timeout per command (default: 30s)')
    parser.add_argument('--verbose', '-v', action='store_true')

    args = parser.parse_args()

    hosts = resolve_hosts([args.host], False)
    if not hosts:
        log_error("Host not found.")
        sys.exit(1)
    host = hosts[0]

    if args.undrain:
        log_info(f"Undrain {Colors.bold(host['name'])} — restoring service")
        ok = undrain(host, args.k3s, args.update_inventory, args.timeout, args.verbose)
        sys.exit(0 if ok else 1)

    # Show current state
    log_info(f"Current state of {Colors.bold(host['name'])}:")
    show_status(host, args.timeout, args.verbose)

    # Confirm
    if not args.force:
        reply = input(
            f"{Colors.warn('WARNING')} This will stop all containers on "
            f"{Colors.bold(host['name'])}. Continue? [y/N] "
        ).strip().lower()
        if reply != 'y':
            print("Aborted.")
            sys.exit(0)

    ok = drain(host, args.k3s, args.update_inventory, args.timeout, args.verbose)
    print()
    if ok:
        log_ok(f"{host['name']} drained successfully")
        if not args.update_inventory:
            log_warn("Remember to update the inventory manually or re-run with --update-inventory")
    else:
        log_error(f"Drain of {host['name']} completed with errors")
        sys.exit(1)


if __name__ == '__main__':
    main()
