#!/usr/bin/env python3
"""Loopback WeChat preview with an explicit UTF-8 X11 clipboard bridge."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import subprocess
import threading
import tempfile
import time

MAX_TEXT = 1024 * 1024

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信桌面预览</title><style>
*{box-sizing:border-box}body{margin:0;background:#14171c;color:#edf0f5;font:14px system-ui}
header{height:44px;display:flex;align-items:center;gap:12px;padding:0 16px;border-bottom:1px solid #343942}
button,a{font:inherit;color:inherit}button{padding:6px 12px;background:#303741;border:1px solid #535c69;border-radius:5px;cursor:pointer}
a{margin-left:auto;font-size:12px}iframe{width:100%;height:calc(100vh - 44px);border:0;display:block}
aside{position:absolute;z-index:2;right:16px;top:52px;width:min(440px,calc(100vw - 32px));background:#242a33;padding:16px;border:1px solid #535c69;border-radius:8px;box-shadow:0 8px 32px #0008}
aside[hidden]{display:none}textarea{width:100%;height:150px;resize:vertical;margin:8px 0;padding:10px;font:14px system-ui;border-radius:5px}
p{line-height:1.6;margin:6px 0 10px;color:#cbd2dc}#status{display:block;min-height:22px;margin-top:10px;color:#b4dfbd}
</style><header><strong>微信桌面</strong><button id="toggle">复制与粘贴</button>
<a href="__VNC__" target="_blank" rel="noopener">直接打开桌面 ↗</a></header>
<aside id="panel" hidden><strong>中文剪贴板</strong>
<p>本机 → 微信：在下方粘贴文字，点击“传入远端”，然后在微信输入框按 Ctrl+V。<br>
微信 → 本机：在微信内复制文字，再点击“复制到本机”。</p>
<textarea id="text" aria-label="剪贴板文字" placeholder="在这里粘贴或查看文字"></textarea>
<button id="send">传入远端</button> <button id="read">复制到本机</button>
<span id="status" role="status"></span></aside><iframe title="微信远程桌面" src="__VNC__"></iframe>
<script>
const token=__TOKEN__, text=document.querySelector('#text'),status=document.querySelector('#status');
document.querySelector('#toggle').onclick=()=>{const p=document.querySelector('#panel');p.hidden=!p.hidden;};
async function transfer(value){const r=await fetch('/clipboard',{method:'POST',headers:{'Content-Type':'application/json','X-Preview-Token':token},body:JSON.stringify(value)});const j=await r.json();if(!r.ok)throw Error(j.error||'操作失败');return j;}
document.querySelector('#send').onclick=async()=>{try{await transfer({action:'write',text:text.value});status.textContent='已传入远端，请在微信输入框按 Ctrl+V';}catch(e){status.textContent=e.message;}};
document.querySelector('#read').onclick=async()=>{try{const j=await transfer({action:'read'});text.value=j.text;try{await navigator.clipboard.writeText(j.text);status.textContent='已复制到本机';}catch{ text.focus();text.select();status.textContent='已读取，请按 Ctrl+C 复制到本机';}}catch(e){status.textContent=e.message;}};
</script></html>"""


class Clipboard:
    def __init__(self, display: str):
        self.env = dict(os.environ, DISPLAY=display)
        self.lock = threading.Lock()
        self.owner: subprocess.Popen | None = None

    def _read(self) -> str:
        # File-backed output bounds Python memory even for a hostile selection.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ['xclip', '-selection', 'clipboard', '-out', '-target', 'UTF8_STRING'],
                env=self.env, stdout=output, stderr=subprocess.DEVNULL,
                timeout=3, check=False,
            )
            if result.returncode:
                return ''
            output.seek(0)
            payload = output.read(MAX_TEXT + 1)
        if len(payload) > MAX_TEXT:
            raise ValueError('剪贴板文字过长')
        return payload.decode('utf-8')

    def read(self) -> str:
        with self.lock:
            return self._read()

    @staticmethod
    def _stop(proc):
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)

    def close(self):
        with self.lock:
            self._stop(self.owner)
            self.owner = None

    def write(self, value: str) -> None:
        payload = value.encode('utf-8')
        if len(payload) > MAX_TEXT:
            raise ValueError('剪贴板文字过长')
        with self.lock:
            # A file avoids blocking on a pipe to a stalled X server. Keep the
            # foreground xclip owner alive until the next native Copy action.
            with tempfile.TemporaryFile() as source:
                source.write(payload)
                source.seek(0)
                proc = subprocess.Popen(
                    ['xclip', '-selection', 'clipboard', '-in', '-target', 'UTF8_STRING', '-quiet'],
                    env=self.env, stdin=source, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            previous, self.owner = self.owner, proc
            try:
                for _ in range(30):
                    if proc.poll() is not None:
                        raise OSError('Clipboard owner exited')
                    if self._read() == value:
                        break
                    time.sleep(0.02)
                else:
                    raise OSError('Clipboard ownership not confirmed')
            except BaseException:
                self._stop(proc)
                self.owner = previous
                raise
            self._stop(previous)


def make_server(port: int, novnc_port: int, display: str) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(32)
    clipboard = Clipboard(display)
    vnc = f'http://127.0.0.1:{novnc_port}/vnc.html?autoconnect=1&resize=scale&reconnect=1'
    page = PAGE.replace('__VNC__', vnc.replace('&', '&amp;')).replace('__TOKEN__', json.dumps(token)).encode()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args):
            pass  # Never log clipboard contents or capability tokens.

        def reply(self, code: int, payload: bytes, mime: str = 'application/json'):
            self.send_response(code)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src " + f'http://127.0.0.1:{novnc_port}' + "; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host():
                self.reply(403, b'{}')
            elif self.path == '/':
                self.reply(200, page, 'text/html')
            elif self.path == '/health':
                self.reply(200, json.dumps({'service': 'wechat-preview', 'ok': True, 'display': display, 'novnc_port': novnc_port}).encode())
            else:
                self.reply(404, b'{}')

        def do_POST(self):
            if (not self.valid_host() or self.path != '/clipboard'
                    or self.headers.get('Origin') != f'http://127.0.0.1:{self.server.server_port}'
                    or not hmac.compare_digest(self.headers.get('X-Preview-Token', '').encode(), token.encode())):
                self.reply(403, b'{}')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_TEXT * 6 + 128:
                    raise ValueError('剪贴板文字过长')
                data = json.loads(self.rfile.read(length))
                if data.get('action') == 'read':
                    result = {'text': clipboard.read()}
                elif data.get('action') == 'write' and isinstance(data.get('text'), str):
                    clipboard.write(data['text'])
                    result = {'ok': True}
                else:
                    raise ValueError('不支持的操作')
                self.reply(200, json.dumps(result, ensure_ascii=False).encode())
            except (ValueError, UnicodeError, AttributeError):
                self.reply(400, json.dumps({'error': '文字格式无效或超过 1 MiB'}, ensure_ascii=False).encode())
            except (OSError, subprocess.SubprocessError):
                self.reply(503, json.dumps({'error': '暂时无法访问远端剪贴板，请重试'}, ensure_ascii=False).encode())

    class Server(ThreadingHTTPServer):
        def server_close(self):
            clipboard.close()
            super().server_close()

    server = Server(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=6108)
    parser.add_argument('--novnc-port', type=int, default=6107)
    parser.add_argument('--display', default=':97')
    args = parser.parse_args()
    with make_server(args.port, args.novnc_port, args.display) as server:
        server.serve_forever()
