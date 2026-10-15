"""Report requested OS version floors, not a certificate of application compatibility."""
from __future__ import annotations
import argparse
import json
import platform
import re
import sys
from pathlib import Path


def version(value: str) -> tuple[int, ...] | None:
    if re.search(r"[-+.]((?:alpha|beta|rc|dev)(?:\d|$))", value, re.IGNORECASE):
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-+].*)?", value)
    return tuple(map(int, match[1].split('.'))) if match else None


def at_least(value: str, floor: tuple[int, ...]) -> bool:
    parsed = version(value)
    if parsed is None:
        return False
    size = max(len(parsed), len(floor))
    return parsed + (0,) * (size - len(parsed)) >= floor + (0,) * (size - len(floor))


def assess(observed: dict) -> dict:
    checks: dict[str, bool] = {}
    system = observed.get('system')
    if system == 'Windows':
        checks['windows_workstation'] = observed.get('windows_product_type') == 1
        checks['windows_11_or_newer_build'] = at_least(str(observed.get('windows_build', '')), (22000,))
    elif system == 'Darwin':
        checks['macos_26_or_newer'] = at_least(str(observed.get('macos_version', '')), (26,))
    elif system == 'Linux':
        checks['linux_kernel_6_18_56_or_newer'] = at_least(str(observed.get('release', '')), (6, 18, 56))
        distro = observed.get('distro_id')
        floors = {'ubuntu': (26, 4), 'debian': (13,), 'fedora': (44,)}
        checks['requested_linux_distribution'] = distro in floors
        checks['distribution_version'] = distro in floors and at_least(str(observed.get('distro_version', '')), floors[distro])
    else:
        checks['recognized_os'] = False
    checks['python_3_10_or_newer'] = at_least(str(observed.get('python_version', '')), (3, 10))
    matched = all(checks.values())
    emulated = bool(observed.get('wsl') or observed.get('container_marker'))
    return {'schema_version': 1, 'observed': observed, 'checks': checks,
            'requested_floors_match': matched, 'target_host_eligible': matched and not emulated,
            'application_compatibility': 'not_proven_by_version_probe',
            'kernel_evidence': 'shared_host_kernel' if emulated else 'observed_runtime_kernel_not_independently_attested'}


def observe() -> dict:
    system = platform.system()
    release = platform.release()
    result: dict[str, str | int | bool] = {'system': system, 'release': release, 'machine': platform.machine(),
              'python_version': platform.python_version(), 'python_implementation': platform.python_implementation()}
    if system == 'Windows':
        info = sys.getwindowsversion()
        result.update(windows_product_type=info.product_type, windows_build=str(info.build))
    elif system == 'Darwin':
        result['macos_version'] = platform.mac_ver()[0]
    elif system == 'Linux':
        data = platform.freedesktop_os_release()
        result.update(distro_id=data.get('ID', ''), distro_version=data.get('VERSION_ID', ''),
                      wsl='microsoft' in release.lower(),
                      container_marker=Path('/.dockerenv').exists() or Path('/run/.containerenv').exists())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-target', action='store_true', help='Fail if requested floors or host eligibility are not met; never gates normal application startup')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--expected-system', choices=['Windows', 'Darwin', 'Linux'])
    parser.add_argument('--expected-distro', choices=['ubuntu', 'debian', 'fedora'])
    parser.add_argument('--minimum-distro-version')
    args = parser.parse_args(argv)
    if args.output:
        args.output.unlink(missing_ok=True)
    observed = observe()
    result = assess(observed)
    if args.expected_system:
        result['checks']['expected_system'] = observed['system'] == args.expected_system
    if args.expected_distro:
        result['checks']['expected_distribution'] = observed.get('distro_id') == args.expected_distro
    if args.minimum_distro_version:
        minimum = version(args.minimum_distro_version)
        if minimum is None:
            parser.error('Invalid minimum distro version')
        result['checks']['expected_minimum_distribution'] = at_least(str(observed.get('distro_version', '')), minimum)
    result['requested_floors_match'] = all(result['checks'].values())
    result['target_host_eligible'] = result['requested_floors_match'] and not (observed.get('wsl') or observed.get('container_marker'))
    payload = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
    print(payload, end='')
    return 1 if args.require_target and not result['target_host_eligible'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
