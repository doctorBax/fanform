"""Small dependency-free authenticated HTTP API and static UI."""
from __future__ import annotations
import hashlib
import hmac
import http.cookies
import json
import secrets
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from .core import Controller, CONFIG, SafetyError

UI = Path(__file__).resolve().parent / 'web'


class API(BaseHTTPRequestHandler):
    controller: Controller
    token = secrets.token_urlsafe(32)
    login_failures = {}
    lock = threading.Lock()

    def log_message(self, fmt, *args):
        sys.stderr.write('fanform http: ' + (fmt % args) + '\n')

    def reply(self, status, obj, cookie=None):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if cookie: self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def authorized(self):
        cookie = http.cookies.SimpleCookie()
        try: cookie.load(self.headers.get('Cookie', ''))
        except http.cookies.CookieError: return False
        item = cookie.get('fanform_session')
        return bool(item and hmac.compare_digest(item.value, type(self).token))

    def _body(self):
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise SafetyError('Ожидается application/json')
        try: n = int(self.headers.get('Content-Length', '0'))
        except ValueError: raise SafetyError('Некорректная длина запроса')
        if not 0 < n < 65536: raise SafetyError('Некорректная длина запроса')
        try: return json.loads(self.rfile.read(n))
        except (ValueError, UnicodeDecodeError): raise SafetyError('Некорректный JSON')

    def _origin_ok(self):
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlparse(origin)
            if parsed.netloc != self.headers.get('Host') or parsed.scheme not in ('http', 'https'):
                return False
        return self.headers.get('X-Fanform-Request') == '1'

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html', '/style.css', '/app.js'):
            filename = 'index.html' if path in ('/', '/index.html') else path[1:]
            f = UI / filename
            try: data = f.read_bytes()
            except OSError: return self.reply(404, {'error': 'UI not installed'})
            mime = 'text/html' if filename.endswith('html') else ('text/css' if filename.endswith('css') else 'application/javascript')
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers(); self.wfile.write(data)
            return
        if path == '/api/session':
            return self.reply(200, {'authenticated': self.authorized()})
        if not self.authorized(): return self.reply(401, {'error': 'Требуется авторизация'})
        if path == '/api/state': return self.reply(200, self.controller.inventory())
        return self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._origin_ok(): return self.reply(403, {'error': 'Недопустимый источник запроса'})
        try:
            data = self._body()
            if not isinstance(data, dict): raise SafetyError('Ожидается JSON-объект')
            if path == '/api/login':
                ip = self.client_address[0]
                now = time.monotonic()
                with type(self).lock:
                    failures = [t for t in type(self).login_failures.get(ip, []) if now - t < 60]
                    if len(failures) >= 5: return self.reply(429, {'error': 'Слишком много попыток. Повторите позднее.'})
                password = data.get('password', '')
                stored = self.controller.cfg.get('password', {})
                digest = hashlib.pbkdf2_hmac('sha256', str(password).encode(), bytes.fromhex(stored['salt']), 250000)
                good = hmac.compare_digest(digest.hex(), stored['hash'])
                if not good:
                    with type(self).lock: type(self).login_failures[ip] = failures + [now]
                    return self.reply(401, {'error': 'Неверный пароль'})
                with type(self).lock: type(self).login_failures.pop(ip, None)
                return self.reply(200, {'authenticated': True},
                    'fanform_session=' + type(self).token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
            if not self.authorized(): return self.reply(401, {'error': 'Требуется авторизация'})
            if path == '/api/logout':
                return self.reply(200, {'ok': True}, 'fanform_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if path == '/api/speed':
                result = self.controller.set_speed(data.get('channel'), data.get('percent'))
            elif path == '/api/mode':
                result = self.controller.set_mode(data.get('channel'), data.get('mode'))
            elif path == '/api/firmware':
                result = self.controller.firmware(data.get('channel'))
            elif path == '/api/calibrate':
                result = self.controller.start_calibration(data.get('channel'))
            elif path == '/api/group/create':
                result = self.controller.group_save(data.get('name'), data.get('channels'))
            elif path == '/api/group/delete':
                result = self.controller.group_delete(data.get('id'))
            elif path == '/api/group/speed':
                result = self.controller.group_speed(data.get('id'), data.get('percent'))
            elif path == '/api/failsafe':
                self.controller.failsafe('Ручной аварийный сброс'); result = {'ok': True}
            else:
                return self.reply(404, {'error': 'Not found'})
            return self.reply(200, result)
        except (SafetyError, KeyError, TypeError, ValueError) as e:
            return self.reply(400, {'error': str(e)})
        except OSError as e:
            self.controller.failsafe('Ошибка ввода-вывода')
            return self.reply(503, {'error': str(e)})


def serve():
    controller = Controller()
    API.controller = controller
    host, port = controller.cfg['bind'], int(controller.cfg['port'])
    httpd = ThreadingHTTPServer((host, port), API)
    def stop(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        print(f'Fanform listening on {host}:{port}', flush=True)
        httpd.serve_forever(poll_interval=.2)
    finally:
        controller.shutdown()
        httpd.server_close()

if __name__ == '__main__': serve()
