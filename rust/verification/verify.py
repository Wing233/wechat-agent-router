"""Record the Python service twice, then replay against both implementations."""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

from bridge_cases import EXT, FAKE, REFERENCE, ROOT, TOKEN
from unit_gate import verify_unit

APP = ROOT / 'rust' / 'app'
SOURCE = ['scripts/ncm_bridge.py', 'plugins/music/readonly.py', 'plugins/music/client.py']


def command(*args, report=False, env=None):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=report)
    if result.returncode != 0 and report:
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
    result.check_returncode()
    return json.loads(result.stdout) if report else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rust', type=Path, help='Use an existing Rust binary')
    parser.add_argument('--offline', action='store_true', help='Build using cached crates')
    parser.add_argument('--benchmark', action='store_true')
    parser.add_argument('--unit-gate', action='store_true')
    args = parser.parse_args()
    state = FAKE.parent
    state.mkdir(parents=True, exist_ok=True)
    oracle = Path(tempfile.mkdtemp(prefix='oracle-', dir=state))
    print(f'验证记录目录为 {oracle}', flush=True)
    for source in SOURCE:
        target = oracle / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REFERENCE / source, target)

    executable = args.rust or APP / 'target' / 'release' / f'wechat-agent-music-bridge{EXT}'
    if args.rust is None:
        cargo = ['--manifest-path', APP / 'Cargo.toml', '--target-dir', APP / 'target']
        offline = ['--offline'] if args.offline else []
        env = {**os.environ, 'NCM_BRIDGE_PYTHON_VERSION': platform.python_version(), 'CARGO_INCREMENTAL': '0'}
        command('cargo', 'fmt', '--manifest-path', APP / 'Cargo.toml', '--check')
        command('cargo', 'clippy', *cargo, '--locked', *offline, '--all-targets', '--', '-D', 'warnings', env=env)
        command('cargo', 'test', *cargo, '--locked', *offline, env=env)
        command('cargo', 'build', *cargo, '--locked', *offline, '--release', env=env)

    command('rustc', '--edition', '2021', ROOT / 'rust/verification/fake_ncm.rs', '-o', FAKE)
    fixture = FAKE.relative_to(ROOT)
    (oracle / fixture).parent.mkdir(parents=True)
    shutil.copy2(FAKE, oracle / fixture)
    inputs = oracle / 'requests.jsonl'
    cases = ROOT / 'rust/verification/bridge_cases.py'
    command(sys.executable, cases, '--inputs', inputs)
    config = ['--port', '{port}', '--timeout', '1', '--token', TOKEN, '--bin', fixture.as_posix()]
    analysis = command('rust-anything', 'analyze', oracle, '--lang', 'python', '--whole-app', '--json', report=True)
    recorded = command('rust-anything', 'app', 'record', '--http', '--no-traps', '--name',
                       'wechat-agent-music-bridge', '--inputs', inputs, '--json', oracle,
                       '--', sys.executable, 'scripts/ncm_bridge.py', *config, report=True)
    if recorded['noise'] or recorded['unstable'] or recorded['timed_out']:
        raise RuntimeError(f'Unstable source recording: {recorded}')
    original = command('rust-anything', 'app', 'replay', oracle, '--json', report=True)
    binary = shlex.join([str(executable.resolve()), *config])
    port = command('rust-anything', 'app', 'replay', oracle, '--build', binary, '--json', report=True)
    assert recorded['cases'] == original['passed'] == port['passed'] == port['total']
    stats = {'python': platform.python_version(), 'platform': platform.platform(),
             'units': analysis['whole_app']['pieces'], 'record': recorded, 'original': original,
             'rust': port}
    (state / 'latest.json').write_text(json.dumps(stats, indent=2), encoding='utf-8')
    print(f"rust-anything app replay {port['passed']}/{port['total']}", flush=True)
    benchmark = ['--benchmark'] if args.benchmark else []
    command(sys.executable, cases, '--rust', executable.resolve(), *benchmark)
    if args.unit_gate:
        verify_unit(command, oracle, APP, FAKE, offline=args.offline)


if __name__ == '__main__':
    main()
