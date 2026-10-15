import importlib.util
from pathlib import Path
import pytest

SPEC = importlib.util.spec_from_file_location('platform_check', Path(__file__).resolve().parents[1] / 'scripts/platform_check.py')
if SPEC is None or SPEC.loader is None:
    raise ImportError('Platform-check module loader unavailable')
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def sample(system, **fields):
    return {'system': system, 'python_version': '3.13.12', **fields}


@pytest.mark.parametrize('fields', [
    sample('Windows', windows_product_type=1, windows_build='22000'),
    sample('Windows', windows_product_type=1, windows_build='26200'),
    sample('Darwin', macos_version='26.0'),
    sample('Darwin', macos_version='27.1'),
    sample('Linux', release='6.18.56-generic', distro_id='ubuntu', distro_version='26.04'),
    sample('Linux', release='7.0.1', distro_id='debian', distro_version='13'),
    sample('Linux', release='6.19.0', distro_id='fedora', distro_version='44'),
    sample('Linux', release='6.19.0', distro_id='fedora', distro_version='45'),
])
def test_requested_floors_are_not_application_acceptance(fields):
    result = CHECK.assess(fields)
    assert result['target_host_eligible']
    assert result['application_compatibility'] == 'not_proven_by_version_probe'


@pytest.mark.parametrize('fields', [
    sample('Windows', windows_product_type=3, windows_build='26100'),
    sample('Windows', windows_product_type=1, windows_build='19045'),
    sample('Windows', windows_build='26200'),
    sample('Darwin', macos_version='15.7'),
    sample('Darwin', macos_version=''),
    sample('Linux', release='6.18.55', distro_id='ubuntu', distro_version='26.04'),
    sample('Linux', release='6.18.56-rc1', distro_id='ubuntu', distro_version='26.04'),
    sample('Linux', release='6.12.0', distro_id='debian', distro_version='13'),
    sample('Linux', release='6.18.56', distro_id='ubuntu', distro_version='24.04'),
    sample('Linux', release='6.18.56', distro_id='fedora', distro_version='rawhide'),
    sample('Linux', release='6.18.56', distro_id='arch', distro_version='rolling'),
    sample('Linux', release='unknown', distro_id='debian', distro_version='13'),
    sample('FreeBSD', release='15.0'),
])
def test_wrong_or_unknown_target_never_becomes_eligible(fields):
    assert not CHECK.assess(fields)['target_host_eligible']


@pytest.mark.parametrize('marker', ['wsl', 'container_marker'])
def test_shared_kernel_is_not_distro_kernel_certification(marker):
    result = CHECK.assess(sample('Linux', release='6.18.56', distro_id='debian', distro_version='13', **{marker: True}))
    assert result['requested_floors_match']
    assert not result['target_host_eligible']
    assert result['kernel_evidence'] == 'shared_host_kernel'


def test_cli_strict_failure_still_preserves_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(CHECK, 'observe', lambda: sample('Darwin', macos_version='25.9'))
    path = tmp_path / 'receipt.json'
    assert CHECK.main(['--require-target', '--output', str(path)]) == 1
    assert 'not_proven_by_version_probe' in path.read_text(encoding='utf-8')


def test_observation_error_invalidates_old_success(tmp_path, monkeypatch):
    path = tmp_path / 'receipt.json'
    path.write_text('{"target_host_eligible":true}', encoding='utf-8')
    def broken():
        raise OSError('os-release unreadable')
    monkeypatch.setattr(CHECK, 'observe', broken)
    with pytest.raises(OSError):
        CHECK.main(['--require-target', '--output', str(path)])
    assert not path.exists()


@pytest.mark.parametrize(('fields', 'args'), [
    (sample('Linux', release='6.18.56', distro_id='ubuntu', distro_version='26.04'), ['--expected-distro', 'fedora']),
    (sample('Linux', release='6.18.56', distro_id='fedora', distro_version='44'), ['--expected-distro', 'fedora', '--minimum-distro-version', '45']),
    (sample('Darwin', macos_version='26.0'), ['--expected-system', 'Linux']),
])
def test_expected_target_cannot_be_satisfied_by_another_supported_os(fields, args, monkeypatch):
    monkeypatch.setattr(CHECK, 'observe', lambda: fields)
    assert CHECK.main(['--require-target', *args]) == 1


def test_ci_target_matrix_does_not_run_self_hosted_code_on_pull_requests():
    import yaml
    root = Path(__file__).resolve().parents[1]
    hosted = yaml.safe_load((root / '.github/workflows/test.yml').read_text(encoding='utf-8'))
    job = hosted['jobs']['backend-platform']
    assert {'macos-26', 'macos-26-intel', 'ubuntu-26.04', 'windows-2025'} <= set(job['strategy']['matrix']['os'])
    manual = yaml.safe_load((root / '.github/workflows/platform-targets.yml').read_text(encoding='utf-8'))
    assert set(manual['on']) == {'workflow_dispatch'}
    job = manual['jobs']['target']
    assert 'github.event.repository.default_branch' in job['if']
    assert job['strategy']['max-parallel'] == 1
    targets = job['strategy']['matrix']['target']
    assert {t['name'] for t in targets} == {'windows11', 'ubuntu26', 'debian13', 'fedora44', 'fedora45'}
    assert all('self-hosted' in t['labels'] and 'vector-lake-ephemeral' in t['labels'] for t in targets)
    assert manual['permissions'] == {'contents': 'read'}
