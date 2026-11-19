"""Record the original with rust-anything and independently compare both servers."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time

from fixture import FixtureServer, PrintPage


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PORT = 19817
ORIGIN = f'http://127.0.0.1:{FIXTURE_PORT}'
TOKEN = 'fixture-render-token'
AUTH = {'x-render-token': TOKEN, 'content-type': 'application/json'}
PDF_DATES = re.compile(rb'/(CreationDate|ModDate) \(D:(\d{14})(Z|[+-]\d{2}\x27\d{2}\x27)\)')


def request(name, method, path, *, headers=None, body=None):
    result = {'name': name, 'method': method, 'path': path}
    if headers is not None:
        result['headers'] = headers
    if body is not None:
        result['body'] = body
    return result


def post(name, body, **kwargs):
    return request(name, 'POST', '/render', headers=AUTH, body=body, **kwargs)


def render(name, page, **fields):
    return post(name, json.dumps({'url': f'{ORIGIN}/print/resume/{page}', **fields}, ensure_ascii=False))


CONTRACT = [
    request('health', 'GET', '/health'),
    request('health-query-is-not-a-route', 'GET', '/health?full=true'),
    request('head-is-not-get', 'HEAD', '/health'),
    request('wrong-method', 'GET', '/render'),
    request('unknown-path', 'PATCH', '/not-found'),
    request('render-query-is-not-a-route', 'POST', '/render?x=1', headers=AUTH),
    request('token-missing', 'POST', '/render'),
    request('token-wrong', 'POST', '/render', headers={'x-render-token': 'other'}),
    post('empty-body', ''),
    post('empty-object', '{}'),
    post('null-body', 'null'),
    post('array-body', '[]'),
    post('primitive-body', '42'),
    post('json-incomplete', '{'),
    post('json-whitespace', '  '),
    post('json-invalid', 'bad'),
    post('json-trailing-property-comma', '{"url":"unused",}'),
    post('json-property-colon-missing', '{"url" "unused"}'),
    post('json-trailing-array-comma', '[1,]'),
    post('json-error-utf16-position', '{\n"😀":1, x:2}'),
    post('json-trailing-text', 'false x'),
    post('not-a-url', '{"url":"nope"}'),
    post('url-not-http', '{"url":"file:///print/resume/test"}'),
    post('host-not-allowed', '{"url":"https://example.invalid/print/resume/test"}'),
    post('print-path-required', json.dumps({'url': f'{ORIGIN}/resume'})),
    post('normalized-path-checked', json.dumps({'url': f'{ORIGIN}/print/resume/../other'})),
    post('url-object-coercion', '{"url":{"toString":null}}'),
    post('json-lone-surrogate', r'{"other":"\ud800"}'),
]

BROWSER = [
    render('overflow', 'overflow'),
    render('upstream-not-found', 'missing'),
    render('upstream-unavailable', 'unavailable'),
    render('navigation-timeout', 'timeout', timeout_ms=2),
    render('pdf-timeout-not-numeric', 'single', timeout_ms='invalid'),
    render('pdf-a4', 'single'),
    render('pdf-at-overflow-boundary', 'edge'),
    render('pdf-two-pages', 'multi'),
    render('pdf-css-page-size', 'css-size'),
    render('pdf-absent-selector', 'no-paper'),
    render('pdf-redirect', 'redirect'),
    render('pdf-allowed-resource', 'resource-allowed'),
    render('pdf-blocked-resource', 'resource-blocked'),
    render('pdf-delayed-stylesheet', 'resource-slow'),
    render('pdf-delayed-frame', 'iframe'),
]

PDF_EXPECTED = {
    'pdf-a4': (235, 1),
    'pdf-at-overflow-boundary': (1130, 2),
    'pdf-two-pages': (950, 2),
    'pdf-css-page-size': (235, 1),
    'pdf-absent-selector': (0, 1),
    'pdf-redirect': (235, 1),
    'pdf-allowed-resource': (777, 1),
    'pdf-blocked-resource': (235, 1),
    'pdf-timeout-not-numeric': (235, 1),
    'pdf-delayed-stylesheet': (860, 1),
    'pdf-delayed-frame': (845, 1),
}


def fixture_env():
    return {**os.environ, 'RENDER_TOKEN': TOKEN, 'ALLOWED_ORIGINS': f' , {ORIGIN} , '}


@contextmanager
def fixture():
    with FixtureServer(('127.0.0.1', FIXTURE_PORT), PrintPage) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def exchange(port, case):
    http = HTTPConnection('127.0.0.1', port, timeout=70)
    try:
        body = case.get('body', None)
        http.request(case['method'], case['path'], body.encode() if body is not None else None, case.get('headers', {}))
        response = http.getresponse()
        raw = response.read()
        headers = {name.lower(): value for name, value in response.getheaders()}
        stable_headers = {name: headers.get(name) for name in ['content-type', 'content-length', 'cache-control']}
        if case['method'] != 'HEAD':
            assert int(stable_headers['content-length']) == len(raw), (case['name'], stable_headers)
        return response.status, stable_headers, raw
    finally:
        http.close()


@contextmanager
def running(command, *, isolated=False):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryFile() as log, tempfile.TemporaryDirectory() as workspace:
        env = {**fixture_env(), 'PORT': str(port)}
        if isolated:
            env['PATH'] = ''
        process = subprocess.Popen(command, cwd=workspace if isolated else ROOT, env=env,
                                   stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if exchange(port, CONTRACT[0])[0] == 200:
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError('service did not start')
            yield port
        except BaseException:
            log.seek(0)
            print(log.read().decode(errors='replace'))
            raise
        finally:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def normalized_pdf(pdf):
    def normalize(match):
        label, digits, timezone = match.groups()
        return b'/' + label + b' (D:' + b'0' * len(digits) + timezone + b')'

    result, count = PDF_DATES.subn(normalize, pdf)
    assert count == 2 and len(result) == len(pdf), 'unexpected PDF metadata'
    return result


def pdf_data(response):
    output = json.loads(response[2])
    assert list(output) == ['overflow', 'height', 'pages', 'bytes', 'pdf_base64']
    pdf = base64.b64decode(output.pop('pdf_base64'), validate=True)
    assert output['bytes'] == len(pdf) and pdf.startswith(b'%PDF-')
    return output, pdf


def pdf_layout(original, rust):
    # Independent comparison of dimensions, extracted text, and rasterized pages.
    import pymupdf

    with pymupdf.open(stream=original) as left, pymupdf.open(stream=rust) as right:
        assert left.page_count == right.page_count
        for a, b in zip(left, right, strict=True):
            assert (a.mediabox, a.cropbox, a.rotation) == (b.mediabox, b.cropbox, b.rotation)
            assert a.get_text() == b.get_text()
            assert a.get_pixmap().samples == b.get_pixmap().samples


def check_busy(port):
    with ThreadPoolExecutor() as pool:
        work = pool.submit(exchange, port, render('slow-render', 'slow'))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            active = exchange(port, CONTRACT[0])
            if json.loads(active[2])['busy']:
                break
            time.sleep(.01)
        else:
            raise AssertionError('health never reported busy')
        busy = exchange(port, render('contending-render', 'single'))
        unauthorized = exchange(port, CONTRACT[6])
        assert busy[0] == 429 and unauthorized[0] == 401
        assert work.result()[0] == 200
    done = exchange(port, CONTRACT[0])
    assert json.loads(done[2]) == {'ok': True, 'busy': False}
    return active, busy, unauthorized, done


def compare(binary, smoke, isolated):
    selected = CONTRACT if smoke else CONTRACT + BROWSER
    with running(['node', 'server.js']) as old, running([str(binary)], isolated=isolated) as new:
        if not smoke:
            sample = next(case for case in BROWSER if case['name'] == 'pdf-a4')
            first_meta, first = pdf_data(exchange(old, sample))
            time.sleep(1.1)
            second_meta, second = pdf_data(exchange(old, sample))
            assert first != second and first_meta == second_meta and normalized_pdf(first) == normalized_pdf(second)
            print('original repeated across seconds: only the two PDF date fields changed', flush=True)
        boundary = [post('body-at-limit', '{}' + ' ' * (65536 - 2)), post('body-above-limit', '{}' + ' ' * (65537 - 2))]
        pdfs = 0
        durations = [0.0, 0.0]
        for case in selected + boundary:
            responses = []
            for index, port in enumerate([old, new]):
                start = time.monotonic()
                responses.append(exchange(port, case))
                durations[index] += time.monotonic() - start
            a, b = responses
            assert a[:2] == b[:2], (case['name'], a[:2], b[:2], a[2][:300], b[2][:300])
            if case['name'] in PDF_EXPECTED:
                assert a[0] == 200, (case['name'], a[0], a[2][:300])
                a_meta, a_pdf = pdf_data(a)
                b_meta, b_pdf = pdf_data(b)
                assert a_meta == b_meta, (case['name'], a_meta, b_meta)
                assert not a_meta['overflow']
                assert (a_meta['height'], a_meta['pages']) == PDF_EXPECTED[case['name']], (case['name'], a_meta)
                assert normalized_pdf(a_pdf) == normalized_pdf(b_pdf), case['name']
                pdf_layout(a_pdf, b_pdf)
                pdfs += 1
            else:
                assert a[2] == b[2], (case['name'], a[2][:300], b[2][:300])
            print(f'{case["name"]}: {a[0]} matched', flush=True)
        if not smoke:
            assert check_busy(old) == check_busy(new)
            print('parallel render/health/auth/recovery matched', flush=True)
        print(f'{len(selected) + len(boundary)} HTTP cases, {pdfs} PDF comparisons, '
              f'original {durations[0]:.2f}s / Rust {durations[1]:.2f}s', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true', help='only verify HTTP without Chromium')
    parser.add_argument('--isolated', action='store_true', help='run Rust with an empty PATH and working directory')
    parser.add_argument('action', choices=['record', 'replay', 'compare', 'lift', 'front-door'])
    parser.add_argument('binary', nargs='?', type=Path)
    args = parser.parse_args()
    with fixture():
        if args.action == 'compare':
            compare(args.binary.resolve(), args.smoke, args.isolated)
        elif args.action == 'record':
            # Keep stable non-PDF cases as portable rust-anything fixtures. Compare PDF bytes independently.
            cases = CONTRACT if args.smoke else CONTRACT + [case for case in BROWSER if not case['name'].startswith('pdf-')]
            with tempfile.TemporaryDirectory() as folder:
                inputs = Path(folder) / 'inputs.jsonl'
                inputs.write_text(''.join(json.dumps(case) + '\n' for case in cases), encoding='utf-8')
                subprocess.run(['rust-anything', 'app', 'record', '.', '--http', '--no-traps', '--inputs',
                                str(inputs), '--', 'node', 'server.js'], cwd=ROOT, env=fixture_env(), check=True)
        else:
            cmd = ['rust-anything', 'app', args.action, '.']
            if args.binary is not None:
                cmd.extend(['--build', shlex.quote(args.binary.resolve().as_posix())])
            subprocess.run(cmd, cwd=ROOT, env=fixture_env(), check=True)


if __name__ == '__main__':
    main()
