#!/usr/bin/env python3
"""
rollout.py - Rolling Docker deployment
After Dark Systems - Ops Utils

Deploys a new Docker image across a fleet of hosts one at a time (or in
configurable batches), with optional HTTP health checks between each host.
On failure it stops the rollout and leaves the last host in a known state.

Usage:
  rollout.py --hosts web-01,web-02,web-03 --image nginx:1.25 --name web --port 80:80
  rollout.py --all --dc prod --image myapp:v2.1 --name myapp --port 8080:8080 \\
             --health-url http://{host}:8080/health
  rollout.py --all --image myapp:v2.1 --name myapp --env APP_ENV=prod \\
             --restart unless-stopped --concurrency 2
"""

import argparse
import sys
import os
import time
import urllib.request
import urllib.error
import ssl
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, log_info, log_ok, log_warn, log_error, log_step,
                  resolve_hosts, ssh_run)


def _run_step(host: dict, cmd: str, timeout: int, verbose: bool,
              label: str) -> tuple[bool, str]:
    """Run a single remote step. Returns (ok, output_or_error)."""
    stdout, err = ssh_run(host, cmd, timeout, verbose)
    if err:
        return False, err
    return True, stdout.strip()


def deploy_host(host: dict, image: str, name: str, ports: list[str],
                envs: list[str], restart: str, extra_args: str,
                health_url: str | None, health_timeout: int,
                health_retries: int, ssh_timeout: int, verbose: bool) -> tuple[bool, str]:
    """Full deploy sequence on a single host. Returns (success, message)."""
    hostname = host['name']

    # Build docker run flags
    port_flags = ' '.join(f'-p {p}' for p in ports)
    env_flags  = ' '.join(f'-e {e}' for e in envs)
    restart_flag = f'--restart {restart}' if restart else ''

    steps = [
        ('pull',         f"docker pull {image}",                                   90),
        ('stop',         f"docker stop {name} 2>/dev/null || true",                30),
        ('rm',           f"docker rm   {name} 2>/dev/null || true",                15),
        ('run',          f"docker run -d --name {name} {port_flags} {env_flags} "
                         f"{restart_flag} {extra_args} {image}",                   30),
    ]

    for label, cmd, timeout in steps:
        log_step(f"[{hostname}] {label}…")
        ok, out = _run_step(host, cmd, max(ssh_timeout, timeout), verbose, label)
        if not ok:
            return False, f"{label} failed: {out}"
        if verbose and out:
            print(f"         {out[:120]}")

    # Health check
    if health_url:
        url = health_url.replace('{host}', host['hostname'])
        log_step(f"[{hostname}] health check → {url}")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        for attempt in range(1, health_retries + 1):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'mgmt-utils/rollout'})
                with urllib.request.urlopen(req, timeout=health_timeout, context=ctx) as resp:
                    if 200 <= resp.status < 300:
                        log_ok(f"[{hostname}] healthy ({resp.status})")
                        return True, 'ok'
                    if attempt == health_retries:
                        return False, f"health check returned {resp.status}"
            except urllib.error.HTTPError as e:
                if 200 <= e.code < 300:
                    return True, 'ok'
                if attempt == health_retries:
                    return False, f"health check HTTP {e.code}"
            except Exception as exc:
                if attempt == health_retries:
                    return False, f"health check failed: {exc}"
            time.sleep(3)

    return True, 'ok'


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Rolling Docker deployment across a fleet of hosts.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)

    # Target
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument('--hosts', metavar='h1,h2,...',
                     help='Comma-separated list of hosts')
    grp.add_argument('--all', dest='all_hosts', action='store_true',
                     help='All active hosts from inventory / SSH config')
    parser.add_argument('--dc', metavar='NAME', default=None)

    # Container spec
    parser.add_argument('--image',   required=True, metavar='IMAGE:TAG')
    parser.add_argument('--name',    required=True, metavar='NAME',
                        help='Container name')
    parser.add_argument('--port',    action='append', default=[], dest='ports',
                        metavar='HOST:CONTAINER',
                        help='Port mapping (repeatable: --port 80:80 --port 443:443)')
    parser.add_argument('--env',     action='append', default=[], dest='envs',
                        metavar='KEY=VAL',
                        help='Environment variable (repeatable)')
    parser.add_argument('--restart', default='unless-stopped',
                        help='Docker restart policy (default: unless-stopped)')
    parser.add_argument('--docker-args', default='', metavar='ARGS',
                        help='Extra arguments passed verbatim to docker run')

    # Health check
    parser.add_argument('--health-url', default=None, metavar='URL',
                        help='Health URL to probe after deploy (use {host} for host IP)')
    parser.add_argument('--health-timeout', type=int, default=10,
                        help='Health check HTTP timeout (default: 10s)')
    parser.add_argument('--health-retries', type=int, default=5,
                        help='Health check retry attempts (default: 5)')

    # Rollout control
    parser.add_argument('--concurrency', type=int, default=1,
                        help='Hosts to deploy in parallel (default: 1 = serial)')
    parser.add_argument('--timeout', type=int, default=30,
                        help='SSH timeout per command (default: 30s)')
    parser.add_argument('--verbose', '-v', action='store_true')

    args = parser.parse_args()

    if args.hosts:
        hosts = resolve_hosts(args.hosts.split(','), False)
    else:
        hosts = resolve_hosts(None, True, args.dc)

    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    log_info(f"Rolling out {Colors.bold(args.image)} → {Colors.bold(args.name)} "
             f"on {len(hosts)} host(s)  [concurrency={args.concurrency}]")
    if args.health_url:
        log_info(f"Health check: {args.health_url}")
    print()

    passed = failed = 0

    # Process hosts in batches of --concurrency
    from concurrent.futures import ThreadPoolExecutor, as_completed

    for batch_start in range(0, len(hosts), args.concurrency):
        batch = hosts[batch_start:batch_start + args.concurrency]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {
                pool.submit(
                    deploy_host, h, args.image, args.name, args.ports, args.envs,
                    args.restart, args.docker_args, args.health_url,
                    args.health_timeout, args.health_retries, args.timeout, args.verbose
                ): h
                for h in batch
            }
            for fut in as_completed(futures):
                host = futures[fut]
                ok, msg = fut.result()
                if ok:
                    log_ok(f"[{host['name']}] deployed successfully")
                    passed += 1
                else:
                    log_error(f"[{host['name']}] FAILED: {msg}")
                    failed += 1

        if failed:
            log_error(f"Rollout stopped after failure ({passed} succeeded, {failed} failed)")
            sys.exit(1)

    print()
    log_ok(f"Rollout complete — {passed}/{len(hosts)} hosts updated")


if __name__ == '__main__':
    main()
