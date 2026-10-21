"""Exercise the preview HTTP boundary without opening or sending a chat."""
import http.client
import importlib.util
import json
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'agentic_tools/wechat_gui_agent/scripts/wechat_preview.py'
spec = importlib.util.spec_from_file_location('wechat_preview', MODULE)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class FakeClipboard:
    def __init__(self, display):
        self.value = ''
    def read(self):
        return self.value
    def write(self, value):
        if len(value.encode()) > preview.MAX_TEXT:
            raise ValueError()
        self.value = value
    def close(self):
        pass


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(preview, 'Clipboard', FakeClipboard)
        self.patch.start()
        self.server = preview.make_server(0, 6107, ':97')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        _, page = self.request('GET', '/')
        self.token = json.loads(re.search(r'const token=("[^"]+")', page.decode())[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.patch.stop()

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        result = (response.status, response.read())
        client.close()
        return result

    def post(self, value, **overrides):
        headers = {'Origin': f'http://127.0.0.1:{self.port}',
                   'X-Preview-Token': self.token, 'Content-Type': 'application/json'}
        headers.update(overrides)
        return self.request('POST', '/clipboard', json.dumps(value).encode(), headers)

    def test_utf8_round_trip_and_health(self):
        text = '群名：OverTree-中文😀\n连接码：TEST1234'
        self.assertEqual(self.post({'action': 'write', 'text': text})[0], 200)
        code, result = self.post({'action': 'read'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(result)['text'], text)
        code, health = self.request('GET', '/health')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(health)['display'], ':97')

    def test_origin_and_token_required(self):
        for override in ({'Origin': 'https://evil.example'}, {'Origin': ''},
                         {'X-Preview-Token': ''}, {'X-Preview-Token': 'wrong'}):
            with self.subTest(override=override):
                self.assertEqual(self.post({'action': 'read'}, **override)[0], 403)

    def test_rebinding_host_rejected(self):
        self.assertEqual(self.request('GET', '/', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.post({'action': 'read'}, Host='evil.example')[0], 403)

    def test_get_never_exposes_clipboard(self):
        self.post({'action': 'write', 'text': 'PRIVATE_FIXTURE'})
        self.assertEqual(self.request('GET', '/clipboard')[0], 404)
        self.assertNotIn(b'PRIVATE_FIXTURE', self.request('GET', '/')[1])

    def test_invalid_payload_and_utf8_byte_limit(self):
        for value in ([], {'action': 'delete'}, {'action': 'write', 'text': 3},
                      {'action': 'write', 'text': '中' * (preview.MAX_TEXT // 3 + 1)}):
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(self.post(value)[0], 400)

    def test_clipboard_failure_is_reported(self):
        with patch.object(FakeClipboard, 'read', side_effect=OSError('unavailable')):
            self.assertEqual(self.post({'action': 'read'})[0], 503)


if __name__ == '__main__':
    unittest.main()
