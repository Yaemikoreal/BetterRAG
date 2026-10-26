# -*- coding: utf-8 -*-
"""BetterRAG HTTP API —— stdlib ThreadingHTTPServer（规格 §13 全端点）。

零第三方依赖。GET / 跳转 /app（三页面单文件应用）。
多线程：Runtime 内部 RLock 串行化全部状态变更。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")


class Api:
    """路由表：method path → runtime 方法。"""

    def __init__(self, rt) -> None:
        self.rt = rt

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple:
        rt = self.rt
        if path == "/api/ask" and method == "POST":
            return 200, rt.ask(body.get("question", ""))
        if path == "/api/feedback" and method == "POST":
            return 200, rt.feedback(body.get("question_id", ""), body.get("kind", ""))
        if path == "/api/scan" and method == "POST":
            return 200, rt.scan()
        if path == "/api/exceptions" and method == "GET":
            return 200, rt.exceptions()
        if path.startswith("/api/adjudicate") and method == "POST":
            action = body.get("action") or path.rsplit("/", 1)[-1]
            return 200, rt.adjudicate(body.get("block_id", ""), action)
        if path.startswith("/api/admin/") and method == "POST":
            op = path.rsplit("/", 1)[-1]
            return 200, rt.admin_op(op, **body)
        if path == "/api/alerts" and method == "GET":
            return 200, rt.alerts()
        if path.startswith("/api/amend") and method == "POST":
            action = body.get("action") or path.rsplit("/", 1)[-1]
            return 200, rt.amend(action, body.get("old_id", ""), body.get("new_id", ""))
        if path == "/api/conflicts" and method == "GET":
            return 200, rt.conflicts()
        if path == "/api/conflict/vote" and method == "POST":
            return 200, rt.vote(body.get("defender", ""), body.get("challenger", ""),
                                body.get("choice", ""))
        if path == "/api/clock/jump" and method == "POST":
            return 200, rt.jump(int(body.get("days", 0)))
        if path == "/api/dashboard" and method == "GET":
            return 200, rt.state()
        if path == "/api/state" and method == "GET":
            return 200, rt.state()
        if path == "/api/starmap" and method == "GET":
            return 200, rt.starmap()
        if path == "/api/events" and method == "GET":
            return 200, rt.events(limit=int(query.get("limit", ["120"])[0]),
                                  block_id=query.get("block_id", [""])[0])
        if path == "/api/biography" and method == "GET":
            return 200, rt.biography(query.get("block_id", [""])[0])
        if path == "/api/config" and method == "GET":
            return 200, rt.get_config()
        if path == "/api/config" and method == "POST":
            return 200, rt.set_config(body.get("config", {}))
        if path == "/api/providers" and method == "GET":
            return 200, rt.get_provider()
        if path == "/api/providers" and method == "POST":
            return 200, rt.set_provider(body)
        if path == "/api/providers/test" and method == "POST":
            return 200, rt.test_provider()
        return 404, {"error": f"未知端点：{method} {path}"}


def make_handler(api: Api, log) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, payload) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, filepath: str, ctype: str) -> None:
            try:
                with open(filepath, "rb") as fh:
                    data = fh.read()
            except OSError:
                self._send(404, {"error": f"静态文件缺失：{os.path.basename(filepath)}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            try:
                v = json.loads(raw.decode("utf-8"))
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}

        def _route(self, method: str) -> None:
            u = urlparse(self.path)
            query = parse_qs(u.query)
            if u.path == "/" and method == "GET":
                self.send_response(302)
                self.send_header("Location", "/app")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.path == "/app" and method == "GET":
                self._static(os.path.join(WEB, "app.html"), "text/html")
                return
            if u.path == "/demo" and method == "GET":
                self._static(os.path.join(ROOT, "BetterRAG-demo.html"), "text/html")
                return
            if u.path.startswith("/api/"):
                body = self._body() if method == "POST" else {}
                try:
                    code, payload = api.handle(method, u.path, query, body)
                    self._send(code, payload)
                except KeyError as e:
                    self._send(404, {"error": str(e).strip("'\"")})
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                except Exception as e:  # 退路：任何异常不拖垮服务线程
                    log(f"500 {u.path}: {type(e).__name__}: {e}")
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def log_message(self, fmt, *args):
            log(fmt % args)

    return Handler


def serve(rt, host: str = "127.0.0.1", port: int = 8787) -> None:
    api = Api(rt)

    def log(msg: str) -> None:
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        print(f"[http] {msg}", flush=True)

    httpd = ThreadingHTTPServer((host, port), make_handler(api, log))
    httpd.daemon_threads = True
    print(f"BetterRAG 服务已启动：http://{host}:{port}/app  （虚拟日 D{int(rt.eng.now)}）",
          flush=True)
    print("API 速查：POST /api/ask · /api/feedback · /api/scan · GET /api/dashboard", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        rt.close()
        print("服务已停止，状态已落盘。", flush=True)
