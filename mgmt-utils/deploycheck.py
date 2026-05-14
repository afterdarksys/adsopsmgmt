#!/usr/bin/env python3
"""
deploycheck.py - Pre-deploy gate
After Dark Systems - Ops Utils

Before deploying a container, verify the target host is actually ready:
  1. Port is not already bound
  2. Container name is not already taken
  3. Host has enough free capacity (CPU headroom, RAM, disk)
  4. Image is pullable from the registry

Usage:
  deploycheck.py --host web-01 --image nginx:1.25 --port 8080 --name web
  deploycheck.py --host web-01 --image myrepo/app:v2 --name app \\
                  --sysspec "cpu=2,ram=4,hd=20g"
  deploycheck.py --all --dc prod --image nginx:1.25 --port 80 --name web
"""

import argparse
import json
import subprocess
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from _lib import (Colors, fmt, hr, log_info, log_error, log_step,
                  add_common_args, resolve_hosts, ssh_run, run_parallel)


# ── Inline capacity check (mirrors capacitycheck.py logic) ──────────────────

_CAP_SCRIPT = r"""
CPU_CORES=$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)
LOAD_1=$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)
MEM_TOTAL_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
MEM_AVAIL_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
printf "CPU_CORES=%s\nLOAD_1=%s\nMEM_TOTAL_KB=%s\nMEM_AVAIL_KB=%s\n" \
    "$CPU_CORES" "$LOAD_1" "$MEM_TOTAL_KB" "$MEM_AVAIL_KB"
df -Pk 2>/dev/null \
  | awk 'NR>1 && $1!~/^(tmpfs|devtmpfs|squashfs|overlay)/{
      printf "DISK %s SIZE_KB=%s AVAIL_KB=%s\n", $6, $2, $4}'
"""


def _parse_sysspec(spec: str) -> dict:
    result = {'cpu': None, 'ram_gb': None, 'hd_gb': None}
    for part in spec.split(','):
        k, _, v = part.strip().partition('=')
        k = k.strip().lower(); v = v.strip()
        if k == 'cpu':
            result['cpu'] = int(v)
        elif k == 'ram':
            s = v.lower()
            result['ram_gb'] = float(s.rstrip('g')) if s.endswith('g') else float(s)/1024
        elif k == 'hd':
            s = v.lower()
            result['hd_gb'] = float(s.rstrip('g'))*1024 if s.endswith('t') else float(s.rstrip('g'))
    return result


def _check_capacity(host: dict, spec: dict, timeout: int, verbose: bool) -> list[tuple[bool, str]]:
    checks = []
    stdout, err = ssh_run(host, _CAP_SCRIPT, timeout, verbose)
    if err:
        return [(False, f"capacity check SSH failed: {err}")]

    stats = {'cpu_cores': 0, 'load_1': 0.0, 'mem_avail_kb': 0, 'disks': []}
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith('DISK '):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    stats['disks'].append({
                        'avail_kb': int(parts[3].split('=')[1])
                    })
                except (ValueError, IndexError):
                    pass
        elif '=' in line:
            k, _, v = line.partition('=')
            k = k.strip().upper()
            try:
                if k == 'CPU_CORES': stats['cpu_cores'] = int(v.strip())
                elif k == 'LOAD_1':  stats['load_1']    = float(v.strip())
                elif k == 'MEM_AVAIL_KB': stats['mem_avail_kb'] = int(v.strip())
            except ValueError:
                pass

    if spec.get('cpu'):
        headroom = stats['cpu_cores'] - stats['load_1']
        ok = headroom >= spec['cpu']
        checks.append((ok,
            f"CPU: {stats['cpu_cores']} cores, {headroom:.1f} free "
            f"(need {spec['cpu']}){'  ✓' if ok else ''}"))

    if spec.get('ram_gb'):
        avail = stats['mem_avail_kb'] / 1_048_576
        ok = avail >= spec['ram_gb']
        checks.append((ok,
            f"RAM: {avail:.1f}GB available (need {spec['ram_gb']:.0f}GB)"
            f"{'  ✓' if ok else ''}"))

    if spec.get('hd_gb'):
        best = max((d['avail_kb'] for d in stats['disks']), default=0) / 1_048_576
        ok = best >= spec['hd_gb']
        checks.append((ok,
            f"Disk: {best:.1f}GB free (need {spec['hd_gb']:.0f}GB)"
            f"{'  ✓' if ok else ''}"))

    return checks


# ── Per-check SSH scripts ────────────────────────────────────────────────────

def _port_script(port: int) -> str:
    return f"""
BOUND=$(ss -tlnpH 2>/dev/null | awk '{{split($4,a,":"); if (a[length(a)]=={port}) print 1}}' | head -1)
DOCKER=$(docker ps --format '{{{{.Ports}}}}' 2>/dev/null | grep -c ':{port}->' || echo 0)
[ "$BOUND" = "1" ] && echo "PORT_BOUND=1" || echo "PORT_BOUND=0"
[ "$DOCKER" -gt 0 ] && echo "DOCKER_PORT=1" || echo "DOCKER_PORT=0"
"""


def _name_script(name: str) -> str:
    return f"""
EXISTS=$(docker ps -a --filter name='^/{name}$' --format '{{{{.Names}}}}' 2>/dev/null | head -1)
[ -n "$EXISTS" ] && echo "NAME_TAKEN=1" || echo "NAME_TAKEN=0"
"""


def _image_pullable(image: str, timeout: int) -> tuple[bool, str]:
    """Check if image is pullable using docker manifest inspect (no actual pull)."""
    try:
        result = subprocess.run(
            ['docker', 'manifest', 'inspect', image],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True, f"{image} is accessible in registry"
        err = result.stderr.strip().splitlines()[0] if result.stderr.strip() else 'not found'
        return False, f"image not pullable: {err}"
    except subprocess.TimeoutExpired:
        return False, "registry check timed out"
    except FileNotFoundError:
        return True, "docker not available locally — skipping registry check"
    except Exception as exc:
        return False, str(exc)


# ── Main host check ──────────────────────────────────────────────────────────

def check_host(host: dict, image: str, port: int | None, name: str | None,
               spec: dict | None, timeout: int, verbose: bool) -> dict:
    checks = []
    all_pass = True

    # 1. Port check
    if port is not None:
        stdout, err = ssh_run(host, _port_script(port), timeout, verbose)
        if err:
            checks.append({'check': f'port {port}', 'ok': False, 'detail': err})
            all_pass = False
        else:
            kv = {k: v for k, _, v in (l.partition('=') for l in stdout.splitlines())}
            bound  = kv.get('PORT_BOUND', '0') == '1'
            docker = kv.get('DOCKER_PORT', '0') == '1'
            ok = not (bound or docker)
            all_pass &= ok
            checks.append({
                'check': f'port {port}',
                'ok': ok,
                'detail': 'free' if ok else ('bound by Docker' if docker else 'bound by system process'),
            })

    # 2. Name check
    if name is not None:
        stdout, err = ssh_run(host, _name_script(name), timeout, verbose)
        if err:
            checks.append({'check': f'name "{name}"', 'ok': False, 'detail': err})
            all_pass = False
        else:
            taken = 'NAME_TAKEN=1' in stdout
            ok = not taken
            all_pass &= ok
            checks.append({
                'check': f'name "{name}"',
                'ok': ok,
                'detail': 'available' if ok else f'container named "{name}" already exists',
            })

    # 3. Capacity check
    if spec:
        cap_checks = _check_capacity(host, spec, timeout, verbose)
        for ok, detail in cap_checks:
            all_pass &= ok
            checks.append({'check': 'capacity', 'ok': ok, 'detail': detail})

    return {
        'host':    host['name'],
        'all_ok':  all_pass,
        'checks':  checks,
    }


def print_results(results: list[dict], image: str, image_ok: bool, image_detail: str) -> None:
    print()
    print(Colors.bold(f"Image: {image}"))
    img_col = Colors.ok('✓ pullable') if image_ok else Colors.fail('✗ not pullable')
    print(f"  {img_col}  {image_detail}\n")

    for r in sorted(results, key=lambda x: x['host']):
        status = Colors.ok('READY') if r['all_ok'] else Colors.fail('NOT READY')
        print(Colors.bold(f"{r['host']}") + f"  [{status}]")
        for c in r['checks']:
            mark = Colors.ok('✓') if c['ok'] else Colors.fail('✗')
            print(f"    {mark}  {c['check']:<20} {c['detail']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Pre-flight check before deploying a container.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_common_args(parser)
    parser.add_argument('--image', required=True, metavar='IMAGE:TAG',
                        help='Docker image to deploy')
    parser.add_argument('--port', type=int, default=None, metavar='N',
                        help='Host port the container will bind')
    parser.add_argument('--name', default=None, metavar='NAME',
                        help='Container name to use')
    parser.add_argument('--sysspec', default=None, metavar='SPEC',
                        help='Capacity requirements e.g. "cpu=2,ram=4,hd=20g"')
    args = parser.parse_args()

    hosts = resolve_hosts(args.host, args.all_hosts, args.dc)
    if not hosts:
        log_error("No hosts found.")
        sys.exit(1)

    spec = _parse_sysspec(args.sysspec) if args.sysspec else None

    # Image check runs locally (no SSH needed)
    log_step(f"Checking image: {args.image}")
    image_ok, image_detail = _image_pullable(args.image, args.timeout)

    log_info(f"Running pre-deploy checks on {len(hosts)} host(s)…")
    results = run_parallel(
        lambda h: check_host(h, args.image, args.port, args.name, spec,
                             args.timeout, args.verbose),
        hosts, args.concurrency)

    if args.json_out:
        print(json.dumps({
            'image': args.image, 'image_ok': image_ok, 'image_detail': image_detail,
            'hosts': results,
        }, indent=2))
    else:
        print_results(results, args.image, image_ok, image_detail)

    all_ok = image_ok and all(r['all_ok'] for r in results)
    sys.exit(0 if all_ok else 1)


if __name__ == '__main__':
    main()
