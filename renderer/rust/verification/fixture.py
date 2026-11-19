"""Synthetic print pages shared by the original and the Rust server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import time


class PrintPage(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def reply(self, status, content=b'', content_type='text/html; charset=utf-8', **headers):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/asset.css':
            return self.reply(200, b'.sheet { height: 777px }', 'text/css')
        if path == '/slow-asset.css':
            time.sleep(0.75)
            return self.reply(200, b'.sheet { height: 860px }', 'text/css')
        if path == '/frame':
            time.sleep(0.75)
            return self.reply(200, b'<script>parent.postMessage(845, location.origin)</script>')
        if path == '/print/resume/redirect':
            return self.reply(302, Location='/print/resume/single')
        if path == '/print/resume/missing':
            return self.reply(404, b'missing')
        if path == '/print/resume/unavailable':
            return self.reply(503, b'unavailable')
        if path == '/print/resume/slow':
            time.sleep(0.8)
        if path == '/print/resume/timeout':
            time.sleep(6)

        suffix = path.rsplit('/', 1)[-1]
        height = {'edge': 1130, 'overflow': 1131, 'multi': 950}.get(suffix, 235)
        extra = '<div style="break-before:page">Second printed page</div>' if suffix == 'multi' else ''
        stylesheet = {
            'resource-allowed': '/asset.css',
            'resource-blocked': f'http://localhost:{self.server.server_port}/asset.css',
            'resource-slow': '/slow-asset.css',
        }.get(suffix)
        link = f'<link rel="stylesheet" href="{stylesheet}">' if stylesheet else ''
        sheet = f'<div class="resume-print-only"><div class="resume-paper-inner">'
        sheet += '<div class="sheet"><h1>林明 · Sample Resume</h1><p>Rust · é · 技术项目</p></div></div></div>'
        if suffix == 'no-paper':
            sheet = '<p>Page without the resume selector</p>'
        if suffix == 'iframe':
            extra = '''<script>
                window.addEventListener('message', event => {
                  document.querySelector('.sheet').style.height = `${event.data}px`;
                });
              </script><iframe src="/frame" style="display:none"></iframe>'''
        page_size = '148mm 210mm' if suffix == 'css-size' else 'A4'
        html = f'''<!doctype html><meta charset="utf-8"><title>Resume fixture</title>
        <style>@page {{ size: {page_size}; margin: 0 }} body {{ margin: 0; }}
        .sheet {{ box-sizing: border-box; height: {height}px; overflow: hidden;
                  background: #e8effa; padding: 12px; }}
        @media (max-width: 793px) {{ .sheet {{ height: 41px; }} }}</style>
        {link}{sheet}{extra}'''
        return self.reply(200, html.encode())

    def log_message(self, *_):
        pass


class FixtureServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


if __name__ == '__main__':
    with FixtureServer(('127.0.0.1', int(sys.argv[1])), PrintPage) as server:
        print(server.server_port, flush=True)
        server.serve_forever()
