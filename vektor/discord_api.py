"""Discord: вход через OAuth2, отправка сообщений ботом (в личку и в канал)
и постоянное подключение бота к Gateway, чтобы он был «в сети».

Используется только стандартная библиотека Python — без лишних зависимостей.
Токен бота и client secret берутся из переменных окружения и никуда не логируются.
"""

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://localhost, 1.0) VektorKanban"


class DiscordError(Exception):
    """kind: network — нет связи с Discord; auth — неверные ключи/токен;
    grant — код входа не подошёл (redirect URI, устаревший код); other — прочее."""

    def __init__(self, message, kind="other", status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _proxy_url():
    """Прокси для связи с Discord, если хостинг его блокирует (HTTPS_PROXY в .env)."""
    return (os.environ.get("DISCORD_PROXY") or os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy") or "").strip()


def _opener():
    proxy = _proxy_url()
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    return urllib.request.build_opener()


def _http(method, url, payload=None, headers=None, form=False):
    headers = dict(headers or {})
    headers["User-Agent"] = USER_AGENT
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
    opener = _opener()
    for attempt in range(2):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=15) as resp:
                body = resp.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as err:
            raw = err.read()
            if err.code == 429 and attempt == 0:
                try:
                    retry = float(json.loads(raw).get("retry_after", 1))
                except Exception:
                    retry = 1.0
                time.sleep(min(retry, 10))
                continue
            detail = raw[:300].decode("utf-8", "replace")
            try:
                code = json.loads(raw).get("error", "")
            except Exception:
                code = ""
            if err.code == 401 or code == "invalid_client":
                kind = "auth"
            elif code == "invalid_grant":
                kind = "grant"
            elif err.code == 403 and b"cloudflare" in raw.lower():
                kind = "network"  # Cloudflare заблокировал IP сервера
            else:
                kind = "other"
            raise DiscordError(f"Discord API {err.code}: {detail}", kind, err.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise DiscordError(f"Нет связи с Discord: {reason}", "network") from None
    raise DiscordError("Discord: слишком много запросов", "other", 429)


# ---------------------------------------------------------------- OAuth2 (вход)

def oauth_authorize_url(client_id, redirect_uri, state):
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "none",
    })
    return f"https://discord.com/oauth2/authorize?{query}"


def oauth_fetch_user(client_id, client_secret, redirect_uri, code):
    token = _http("POST", f"{API}/oauth2/token", {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }, form=True)
    access = token.get("access_token")
    if not access:
        raise DiscordError("Discord не выдал токен доступа")
    return _http("GET", f"{API}/users/@me", headers={"Authorization": f"Bearer {access}"})


# ---------------------------------------------------------------- бот: отправка

class DiscordBot:
    def __init__(self, token, channel_id=None):
        self.token = token
        self.channel_id = channel_id
        self._dm_channels = {}
        self._lock = threading.Lock()
        self.online = False

    def _api(self, method, path, payload=None):
        return _http(method, API + path, payload, headers={"Authorization": f"Bot {self.token}"})

    @staticmethod
    def _message(content):
        return {"content": content[:1990], "allowed_mentions": {"parse": []}}

    def send_dm(self, discord_user_id, content):
        with self._lock:
            channel = self._dm_channels.get(discord_user_id)
        if channel is None:
            channel = self._api("POST", "/users/@me/channels", {"recipient_id": str(discord_user_id)})["id"]
            with self._lock:
                self._dm_channels[discord_user_id] = channel
        self._api("POST", f"/channels/{channel}/messages", self._message(content))

    def send_channel(self, content):
        if not self.channel_id:
            return
        self._api("POST", f"/channels/{self.channel_id}/messages", self._message(content))

    # ------------------------------------------------------------ Gateway («в сети»)

    def start_gateway(self, log=print):
        thread = threading.Thread(target=self._gateway_forever, args=(log,), daemon=True,
                                  name="discord-gateway")
        thread.start()
        return thread

    def _gateway_forever(self, log):
        delay = 5
        while True:
            try:
                self._gateway_session(log)
                delay = 5
            except _FatalGateway as err:
                log(f"[Discord] Бот остановлен: {err}")
                self.online = False
                return
            except Exception as err:  # сеть пропала и т.п. — переподключаемся
                log(f"[Discord] Нет соединения с Discord ({err.__class__.__name__}: {str(err)[:150]}), "
                    f"переподключение через {delay} с")
            self.online = False
            time.sleep(delay)
            delay = min(delay * 2, 120)

    def _gateway_session(self, log):
        ws = _WebSocket("gateway.discord.gg", "/?v=10&encoding=json")
        try:
            hello = ws.recv_json(timeout=30)
            if not hello or hello.get("op") != 10:
                raise ConnectionError("нет HELLO")
            interval = hello["d"]["heartbeat_interval"] / 1000.0
            ws.send_json({
                "op": 2,
                "d": {
                    "token": self.token,
                    "intents": 0,
                    "properties": {"os": "windows", "browser": "vektor-kanban", "device": "vektor-kanban"},
                    "presence": {
                        "since": None,
                        "activities": [{"name": "доску задач", "type": 3}],
                        "status": "online",
                        "afk": False,
                    },
                },
            })
            seq = None
            acked = True
            next_beat = time.monotonic() + interval * 0.5
            while True:
                now = time.monotonic()
                if now >= next_beat:
                    if not acked:
                        raise ConnectionError("нет ответа на heartbeat")
                    ws.send_json({"op": 1, "d": seq})
                    acked = False
                    next_beat = now + interval
                msg = ws.recv_json(timeout=1.0)
                if msg is None:
                    continue
                if msg.get("s") is not None:
                    seq = msg["s"]
                op = msg.get("op")
                if op == 11:
                    acked = True
                elif op == 1:
                    ws.send_json({"op": 1, "d": seq})
                elif op in (7, 9):
                    raise ConnectionError("Discord попросил переподключиться")
                elif op == 0 and msg.get("t") == "READY":
                    if not self.online:
                        name = msg["d"]["user"].get("username", "бот")
                        log(f"[Discord] Бот «{name}» в сети")
                    self.online = True
        finally:
            ws.close()


class _FatalGateway(Exception):
    pass


# Коды закрытия, после которых переподключаться бесполезно (неверный токен и т.п.)
FATAL_CLOSE = {4004: "неверный DISCORD_BOT_TOKEN", 4010: "неверный шард", 4011: "нужен шардинг",
               4012: "неверная версия API", 4013: "неверные intents", 4014: "запрещённые intents"}


class _WebSocket:
    """Минимальный WebSocket-клиент (RFC 6455) поверх ssl-сокета."""

    def __init__(self, host, path):
        proxy = _proxy_url()
        if proxy:
            raw = self._via_proxy(proxy, host)
        else:
            raw = socket.create_connection((host, 443), timeout=15)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nUser-Agent: {USER_AGENT}\r\n\r\n"
        ).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("соединение закрыто при рукопожатии")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("Gateway отказал в подключении")
        self.fragments = b""

    @staticmethod
    def _via_proxy(proxy, host):
        """Туннель через HTTP-прокси (метод CONNECT). Поддерживается http://логин:пароль@адрес:порт."""
        parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if parts.scheme not in ("http", "https"):
            raise _FatalGateway("поддерживается только HTTP-прокси (http://адрес:порт)")
        sock = socket.create_connection((parts.hostname, parts.port or 8080), timeout=15)
        head = f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n"
        if parts.username:
            cred = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
            head += "Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode() + "\r\n"
        sock.sendall((head + "\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("прокси закрыл соединение")
            resp += chunk
        if b" 200" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError("прокси отказал: " + resp.split(b"\r\n", 1)[0].decode("latin-1"))
        return sock

    def _send(self, opcode, payload):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_json(self, obj):
        self._send(0x1, json.dumps(obj).encode())

    def _parse_frame(self):
        b = self.buf
        if len(b) < 2:
            return None
        fin, opcode = b[0] & 0x80, b[0] & 0x0F
        length, pos = b[1] & 0x7F, 2
        if length == 126:
            if len(b) < 4:
                return None
            length, pos = struct.unpack(">H", b[2:4])[0], 4
        elif length == 127:
            if len(b) < 10:
                return None
            length, pos = struct.unpack(">Q", b[2:10])[0], 10
        if len(b) < pos + length:
            return None
        payload = b[pos:pos + length]
        self.buf = b[pos + length:]
        return fin, opcode, payload

    def recv_json(self, timeout):
        """Возвращает следующее JSON-сообщение или None, если за timeout ничего не пришло."""
        deadline = time.monotonic() + timeout
        while True:
            frame = self._parse_frame()
            if frame is not None:
                fin, opcode, payload = frame
                if opcode == 0x8:
                    code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1000
                    if code in FATAL_CLOSE:
                        raise _FatalGateway(FATAL_CLOSE[code])
                    raise ConnectionError(f"Gateway закрыл соединение ({code})")
                if opcode == 0x9:
                    self._send(0xA, payload)
                    continue
                if opcode in (0x1, 0x0):
                    self.fragments += payload
                    if fin:
                        data, self.fragments = self.fragments, b""
                        return json.loads(data)
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                raise ConnectionError("соединение закрыто")
            self.buf += chunk

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
