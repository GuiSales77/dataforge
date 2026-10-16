"""Servidor local (somente 127.0.0.1) para rodar o DataForge no próprio computador."""
import json
import mimetypes
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import api, config


class Handler(BaseHTTPRequestHandler):
    server_version = "DataForge"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8", extra=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _guard(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"erro": "acesso permitido apenas localmente"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/"):
                code, res = api.dispatch("GET", u.path)
                return self._send(code, res)
            path = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            root = config.resource_dir().resolve()
            f = (root / path).resolve()
            if not str(f).startswith(str(root)) or not f.is_file() or f.suffix == ".db" or "dados" in f.relative_to(root).parts:
                return self._send(404, {"erro": "não encontrado"})
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"erro": str(e)})

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            code, res = api.dispatch("POST", u.path, body)
            self._send(code, res)
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"erro": f"{type(e).__name__}: {e}"})


def make_server(port: int):
    api.dispatch("GET", "/api/settings")
    for p in range(port, port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler), p
        except OSError:
            continue
    raise RuntimeError("Nenhuma porta livre encontrada")
