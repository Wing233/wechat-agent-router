"""Generate rust-anything inputs and check wire details outside its HTTP recorder."""
import argparse
import concurrent.futures
import errno
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
from statistics import median
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = Path(__file__).resolve().parent / 'reference'
EXT = '.exe' if os.name == 'nt' else ''
FAKE = ROOT / 'rust' / '.verification' / f'fake_ncm{EXT}'
TOKEN = 'parity-fixture-token'
SEARCH = ['search', 'song']


def cases():
    sys.path.insert(0, str(REFERENCE))
    from plugins.music.readonly import READ_ONLY_COMMANDS

    def run(name, args, **kwargs):
        return request(name, 'POST', '/run', json.dumps({'args': args}, ensure_ascii=False), **kwargs)

    yield request('health', 'GET', '/health')
    yield request('get-missing', 'GET', '/run')
    yield request('post-missing-before-auth', 'POST', '/health', '', token=None)
    yield request('health-query', 'GET', '/health?a=1')
    yield request('health-double-slash', 'GET', '//health')
    yield request('run-trailing-slash', 'POST', '/run/', '{}')
    yield request('method-put', 'PUT', '/run')
    yield request('method-options', 'OPTIONS', '/health')
    yield request('method-head', 'HEAD', '/health')
    yield request('missing-auth', 'POST', '/run', '{}', token=None)
    yield request('wrong-auth', 'POST', '/run', '{}', token='Bearer wrong')
    yield request('lowercase-bearer', 'POST', '/run', '{}', token=f'bearer {TOKEN}')
    yield request('trailing-auth-space', 'POST', '/run', '{}', token=f'Bearer {TOKEN} ')
    yield request('empty-body', 'POST', '/run', '')
    yield request('too-large-body', 'POST', '/run', ' ' * 65537)
    body = '{"args":["user","info"]}'
    yield request('body-size-limit', 'POST', '/run', body.ljust(65536))
    yield request('broken-json', 'POST', '/run', '{')
    yield request('bom-json', 'POST', '/run', '\ufeff{}')
    yield request('non-object', 'POST', '/run', '["user","info"]')
    yield request('missing-args', 'POST', '/run', '{}')
    yield request('duplicate-args', 'POST', '/run', '{"args":false,"args":["user","info"]}')
    yield request('nonfinite-unused-field', 'POST', '/run', '{"args":["user","info"],"unused":NaN}')
    yield request('nonfinite-arg', 'POST', '/run', '{"args":["user","info",Infinity]}')
    yield request('nonfinite-key', 'POST', '/run', '{NaN:1}')
    for group, sub in sorted(READ_ONLY_COMMANDS):
        yield run(f'allow-{group}-{sub}', [group, sub])
    for name, args in {
        'one-arg': ['search'], 'not-array': {'0': 'search', '1': 'song'},
        'not-string': SEARCH + [5], 'empty-token': SEARCH + [' \u001f '],
        'max-args': SEARCH + ['x'] * 22, 'too-many-args': SEARCH + ['x'] * 23,
        'max-token': SEARCH + ['🎵' * 200], 'too-long-token': SEARCH + ['🎵' * 201],
        'nul': SEARCH + ['a\0b'], 'newline': SEARCH + ['a\nb'], 'carriage-return': SEARCH + ['a\rb'],
        'output': SEARCH + ['--output'], 'help': SEARCH + ['--help'],
        'short-help': SEARCH + ['-h'], 'user-input': SEARCH + ['--userInput'],
        'write': ['playlist', 'create'], 'playback': ['play', 'song'],
        'trim': [' \u001fsearch\u3000', '\n song\t', ' 晴天🎵 '],
        'argv-literal': SEARCH + ['--keyword', "晴天 (Live) ' 🎵", 'two words', 'd\tb', 'a\\b'],
        'limit-min': SEARCH + ['--limit', '1'], 'limit-max': SEARCH + ['--limit', '200'],
        'limit-low': SEARCH + ['--limit', '0'], 'limit-high': SEARCH + ['--limit', '201'],
        'limit-letters': SEARCH + ['--limit', 'NaN'], 'limit-negative': SEARCH + ['--limit', '-2'],
        'limit-fullwidth': SEARCH + ['--limit', '+２_０_０'],
        'limit-mixed-digits': SEARCH + ['--limit', '١_٢'],
        'limit-long-int': SEARCH + ['--limit', '9' * 199],
        'limit-under': SEARCH + ['--limit', '1__2'], 'limit-missing': SEARCH + ['--limit'],
        'offset-min': SEARCH + ['--offset', '-0'], 'offset-max': SEARCH + ['--offset', '1_000_000'],
        'offset-low': SEARCH + ['--offset', '-1'], 'offset-high': SEARCH + ['--offset', '1000001'],
        'offset-bad': SEARCH + ['--offset', '²'],
    }.items():
        yield run(name, args)
    for fixture in (
        'warning bom tie array string false float nonfinite bigint duplicate null invalid invalid-utf8 empty '
        'config login quota missing unknown empty-error stdout-error stderr-first api api-default '
        'api-list bridge-code success-false success-zero code code-float code-nan code-infinity full-pipes slow'
    ).split():
        yield run(f'cli-{fixture}', SEARCH + [f'fixture-{fixture}'])


def request(name, method, path, body=None, *, token=f'Bearer {TOKEN}'):
    headers = {} if token is None else {'Authorization': token}
    return {'name': name, 'method': method, 'path': path, 'headers': headers, 'body': body}


def raw_case(case):
    body = (case['body'] or '').encode('utf-8')
    headers = ''.join(f'{key}: {value}\r\n' for key, value in case['headers'].items())
    return (f"{case['method']} {case['path']} HTTP/1.1\r\nHost: localhost\r\n"
            f'{headers}Content-Length: {len(body)}\r\n\r\n').encode() + body


def special_cases():
    auth = f'Authorization: Bearer {TOKEN}\r\n'.encode()
    start = b'POST /run HTTP/1.0\r\n' + auth
    body = b'{"args":["user","info"]}'
    length = b'Content-Length: 24\r\n'
    yield 'missing-length', start + b'\r\n'
    yield 'invalid-length', start + b'Content-Length: yes\r\n\r\n'
    yield 'negative-length', start + b'Content-Length: -1\r\n\r\n'
    yield 'underscore-length', start + b'Content-Length: +2_4\r\n\r\n' + body
    yield 'duplicate-length', start + length + b'Content-Length: 0\r\n\r\n' + body
    yield 'partial-body', start + length + b'\r\n{}'
    yield 'invalid-utf8', start + b'Content-Length: 1\r\n\r\n\xff'
    yield 'old-get', b'GET /health\r\n'
    yield 'old-post', b'POST /run\r\n'
    yield 'bad-version', b'GET /health HTTP/nope\r\n\r\n'
    yield 'future-version', b'GET /health HTTP/2.0\r\n\r\n'
    yield 'line-too-long', b'GET /' + b'a' * 65536 + b' HTTP/1.0\r\n\r\n'
    yield 'header-too-long', b'GET / HTTP/1.0\r\nX: ' + b'a' * 65536 + b'\r\n\r\n'
    yield 'too-many-headers', b'GET / HTTP/1.0\r\n' + b'X: a\r\n' * 100 + b'\r\n'


def exchange(port, message):
    with socket.create_connection(('127.0.0.1', port), timeout=10) as conn:
        conn.sendall(message)
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError as error:
            if error.errno not in (errno.ENOTCONN, errno.ECONNRESET):
                raise
        output = bytearray()
        while True:
            try:
                part = conn.recv(65536)
            except ConnectionResetError:
                break
            if not part:
                break
            output.extend(part)
    head, separator, body = bytes(output).partition(b'\r\n\r\n')
    return re.sub(rb'(?m)^Date: [^\r\n]+', b'Date: [clock]', head) + separator + body


def start(command, binary=FAKE, *, token=TOKEN, no_color=None, via_env=False):
    with socket.socket() as conn:
        conn.bind(('127.0.0.1', 0))
        port = conn.getsockname()[1]
    env = os.environ.copy()
    env.pop('NCM_BRIDGE_TOKEN', None)
    env.pop('NCM_CLI_BIN', None)
    env.pop('NO_COLOR', None)
    if no_color is not None:
        env['NO_COLOR'] = no_color
    argv = ['--port', str(port), '--timeout', '0']
    if via_env:
        env.update(NCM_BRIDGE_TOKEN=token, NCM_CLI_BIN=str(binary))
    else:
        argv += ['--bin', str(binary), '--token', token]
    process = subprocess.Popen([*command, *argv], cwd=ROOT, env=env, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError(f'{command} exited with {process.returncode}')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                return process, port
        except OSError:
            time.sleep(.025)
    process.kill()
    process.wait()
    raise RuntimeError(f'{command} failed to start')


def compare(commands, checks, **kwargs):
    servers = []
    try:
        for command in commands:
            servers.append(start(command, **kwargs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for name, original, rust in pool.map(
                lambda item: (item[0], *(exchange(port, item[1]) for _, port in servers)), checks
            ):
                if original != rust:
                    raise AssertionError(f'{name}\nPython: {original[:1000]!r}\nRust:   {rust[:1000]!r}')
    finally:
        for process, _ in servers:
            process.kill()
            process.wait()
    return len(checks)


def compare_cli(commands):
    args_sets = [[], ['--help'], ['-h'], ['--host'], ['--port', 'bad'], ['--timeout', 'bad'],
                 ['--token', ''], ['--unknown'], ['--host', '--port', '12345']]
    env = os.environ.copy()
    env.pop('NCM_BRIDGE_TOKEN', None)
    for args in args_sets:
        results = [subprocess.run([*cmd, *args], cwd=ROOT, env=env, capture_output=True,
                                  timeout=10) for cmd in commands]
        comparable = [(p.returncode, p.stdout.replace(b'\r\n', b'\n'),
                       p.stderr.replace(b'\r\n', b'\n')) for p in results]
        if comparable[0] != comparable[1]:
            raise AssertionError(f'CLI {args!r}: {comparable!r}')
    if os.name != 'nt':
        for command in commands:
            process, _ = start(command)
            try:
                process.send_signal(signal.SIGINT)
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        return len(args_sets) + 1
    return len(args_sets)


def compare_windows_shim(commands):
    if os.name != 'nt' or not shutil.which('node.exe'):
        return 0
    prefix = FAKE.parent / 'npm-shim'
    entry = prefix / 'node_modules/@music163/ncm-cli/dist/index.js'
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text('console.log(JSON.stringify({argv: process.argv.slice(2)}));\n', encoding='ascii')
    shim = prefix / 'ncm-cli.cmd'
    shim.write_text('@echo off\nnode "%~dp0node_modules\\@music163\\ncm-cli\\dist\\index.js" %*\n', encoding='ascii')
    def search(query):
        args = json.dumps({'args': SEARCH + ['--keyword', query]}, ensure_ascii=False)
        return raw_case(request('windows-npm-shim', 'POST', '/run', args))

    matched = compare(commands, [('windows-npm-shim', search('晴天🎵'))], binary=shim)
    outputs = []
    for command in commands:
        process, port = start(command, shim)
        try:
            response = exchange(port, search('晴天 Live 🎵'))
            outputs.append(json.loads(response.split(b'\r\n\r\n', 1)[1])['data']['argv'])
        finally:
            process.kill()
            process.wait()
    prefix = SEARCH + ['--keyword']
    suffix = ['--output', 'json']
    assert outputs == [prefix + ['"晴天', 'Live', '🎵"'] + suffix,
                       prefix + ['晴天 Live 🎵'] + suffix], outputs
    print('Known Windows npm shim difference verified: multiword arguments reach Node intact in Rust')
    return matched


def benchmark(commands, checks):
    for command in commands:
        process, port = start(command)
        try:
            for name, message in checks:
                for _ in range(10):
                    exchange(port, message)
                elapsed = []
                for _ in range(50):
                    before = time.perf_counter()
                    exchange(port, message)
                    elapsed.append(time.perf_counter() - before)
                print(f'{Path(command[0]).name} {name} p50 {median(elapsed) * 1000:.3f} ms')
        finally:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--rust', type=Path)
    parser.add_argument('--benchmark', action='store_true')
    args = parser.parse_args()
    if args.inputs:
        args.inputs.write_text(''.join(json.dumps(case, ensure_ascii=False) + '\n' for case in cases()),
                               encoding='utf-8')
    if args.rust:
        commands = [[sys.executable, str(REFERENCE / 'scripts' / 'ncm_bridge.py')],
                    [str(args.rust.resolve())]]
        checks = [(case['name'], raw_case(case)) for case in cases()] + list(special_cases())
        count = compare(commands, checks)
        run_info = raw_case(request('user-info', 'POST', '/run', '{"args":["user","info"]}'))
        count += compare(commands, [('missing-executable', run_info)], binary=FAKE.with_name('not-installed'))
        count += compare(commands, [('environment', run_info)], via_env=True, no_color='0')
        count += compare(commands, [(f'concurrent-{i}', run_info) for i in range(32)])
        count += compare_windows_shim(commands)
        print(f'{count} raw HTTP comparisons and {compare_cli(commands)} CLI comparisons passed')
        if args.benchmark:
            benchmark(commands, [('health', raw_case(request('health', 'GET', '/health'))), ('run', run_info)])


if __name__ == '__main__':
    main()
