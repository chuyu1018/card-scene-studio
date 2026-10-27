#!/usr/bin/env python3
"""Immediately serve the prebuilt HTML with Python's standard library only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

HTML = Path(__file__).resolve().parents[1] / "assets" / "editor" / "canvas.html"
IDENTITY = hashlib.sha256(str(HTML).encode()).hexdigest()[:16]


class Preview(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        host = self.headers.get("Host")
        if host not in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"):
            self.send_error(403)
            return
        route = self.path.split("?", 1)[0]
        if route == "/health":
            body = json.dumps({"app": "card-scene-studio-static", "identity": IDENTITY}).encode()
            mime = "application/json"
        elif route in ("/", "/canvas.html"):
            body = HTML.read_bytes()
            mime = "text/html; charset=utf-8"
        elif route == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path.home() / ".cache" / "card-scene-studio")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not HTML.is_file():
        parser.error("随技能附带的 canvas.html 缺失，请恢复完整技能文件。")
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    record = args.runtime_dir / f"preview-{IDENTITY}.json"
    if args.serve:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Preview)
        result = {"url": f"http://127.0.0.1:{server.server_port}/", "pid": os.getpid(), "html": str(HTML)}
        tmp = record.with_suffix(".tmp")
        tmp.write_text(json.dumps(result))
        tmp.replace(record)
        server.serve_forever()
        return
    started = time.monotonic()
    try:
        result = json.loads(record.read_text())
        with urlopen(result["url"] + "health", timeout=.25) as response:
            health = json.load(response)
        if health.get("identity") == IDENTITY and health.get("app") == "card-scene-studio-static":
            print(json.dumps({**result, "reused": True, "ready_ms": round((time.monotonic()-started)*1000)}))
            return
    except (OSError, ValueError, KeyError):
        pass
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", "--runtime-dir", str(args.runtime_dir.resolve())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        try:
            result = json.loads(record.read_text())
            with urlopen(result["url"] + "health", timeout=.1) as response:
                health = json.load(response)
            if health.get("identity") == IDENTITY:
                print(json.dumps({**result, "reused": False, "ready_ms": round((time.monotonic()-started)*1000)}))
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.02)
    raise SystemExit("预览没有启动成功，请检查本地 Python。")


if __name__ == "__main__":
    main()
