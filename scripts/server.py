#!/usr/bin/env python3
"""Local editor and project handoff. Image generation runs in the Codex chat."""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

from PIL import Image, ImageOps

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "editor"
MAX_BODY = 100 * 1024 * 1024
MAX_IMAGE = 20 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 40_000_000
ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def store_project(root, payload):
    """Normalize uploads and validate before publishing an immutable project."""
    from render import validate_project
    if not isinstance(payload, dict):
        raise ValueError("项目格式不正确")
    cards = payload.get("cards")
    if not isinstance(cards, list) or not 1 <= len(cards) <= 12:
        raise ValueError("请上传 1 至 12 张图片")
    ident = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    folder = root / "projects" / ident
    inputs = folder / "inputs"
    inputs.mkdir(parents=True)
    try:
        normalized = []
        for i, card in enumerate(cards):
            if not isinstance(card, dict):
                raise ValueError("图片信息不正确，请重新导入")
            raw = card.get("dataUrl", "")
            if not isinstance(raw, str) or not re.match(r"^data:image/(png|jpeg|webp);base64,", raw):
                raise ValueError("仅支持 PNG、JPEG、WebP 图片，请重新上传")
            image_bytes = base64.b64decode(raw.split(",", 1)[1], validate=True)
            if len(image_bytes) > MAX_IMAGE:
                raise ValueError("单张图片不能超过 20 MB")
            with Image.open(io.BytesIO(image_bytes)) as original:
                if original.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError("图片格式不支持")
                original.load()
                image = ImageOps.exif_transpose(original).convert("RGBA")
            digest = hashlib.sha256(image_bytes).hexdigest()[:12]
            src = f"inputs/card-{i+1:02d}-{digest}.png"
            image.save(folder / src)
            item = {k: card[k] for k in ("x", "y", "width", "height", "slot", "rotation") if k in card}
            item.update(id=f"card-{i+1:02d}", name=str(card.get("name", f"图片 {i+1}"))[:160], src=src)
            normalized.append(item)
        data = {k: payload[k] for k in ("version", "mode", "canvas", "grid", "style") if k in payload}
        data.update(cards=normalized, id=ident, created_at=datetime.now(timezone.utc).isoformat())
        if "style" not in data:
            data["style"] = {"preset": "follow", "prompt": ""}
        if not isinstance(data.get("style"), dict):
            raise ValueError("背景设置格式不正确")
        data["style"].setdefault("preset", "follow")
        if len(str(data["style"].get("prompt", ""))) > 5000:
            raise ValueError("背景描述请控制在 5000 字以内")
        data["style"]["backgroundOnly"] = True
        path = folder / "project.json"
        write_json(path, data)
        validate_project(path)
        write_json(folder / "status.json", {"status": "saved"})
        write_json(root / "latest.json", {"id": ident, "project_path": str(path)})
        request = f"请用 $card-scene-studio 读取这个排版项目，生成纯背景并导出整图、原卡合成预览和适用的九宫格切片：{path}"
        (folder / "handoff.txt").write_text(request + "\n", encoding="utf-8")
        from handoff import enqueue
        accepted = enqueue(root, payload.get("handoff_id"), path)
        return {"id": ident, "project_path": str(path), "project_url": f"/?project={ident}", "request_text": request, "preview_url": None, "handoff": {"accepted": accepted}}
    except Exception:
        import shutil
        shutil.rmtree(folder)
        raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def json(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        expected = f"127.0.0.1:{self.server.server_port}"
        host = self.headers.get("Host", "")
        if host not in (expected, f"localhost:{self.server.server_port}"):
            self.json(403, {"error": "仅接受本机访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{expected}", f"http://localhost:{self.server.server_port}"):
            self.json(403, {"error": "请在本地编辑器中操作"})
            return False
        return True

    def file(self, path, base):
        path, base = path.resolve(), base.resolve()
        if not path.is_relative_to(base) or not path.is_file():
            self.json(404, {"error": "文件不存在"})
            return
        import mimetypes
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as stream:
            import shutil
            shutil.copyfileobj(stream, self.wfile)

    def project_folder(self, ident):
        if not ID_RE.fullmatch(ident):
            raise ValueError("项目编号不正确")
        return self.server.root / "projects" / ident

    def do_GET(self):
        if not self.local_request():
            return
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/health":
                return self.json(200, {"app": "card-scene-studio", "root": str(self.server.root), "pid": os.getpid()})
            if path == "/api/handoff":
                from handoff import listener_state
                return self.json(200, listener_state(self.server.root))
            if path == "/api/projects":
                projects = []
                for p in sorted((self.server.root / "projects").glob("*/project.json"), reverse=True):
                    data = read_json(p)
                    state = self.status(p.parent)
                    projects.append({"id": p.parent.name, "project_path": str(p), "mode": data["mode"], "created_at": data.get("created_at", ""), "status": state["status"]})
                return self.json(200, {"projects": projects})
            match = re.fullmatch(r"/api/projects/([^/]+)(/status)?", path)
            if match:
                folder = self.project_folder(match[1])
                if not (folder / "project.json").exists():
                    return self.json(404, {"error": "项目不存在"})
                if match[2]:
                    return self.json(200, self.status(folder))
                data = read_json(folder / "project.json")
                for card in data["cards"]:
                    card["src"] = f"/files/{folder.name}/{card['src']}"
                data["project_path"] = str(folder / "project.json")
                return self.json(200, data)
            if path.startswith("/files/"):
                parts = path.split("/", 3)
                folder = self.project_folder(parts[2])
                return self.file(folder / parts[3], folder)
            return self.file(ASSETS / ("index.html" if path == "/" else path.lstrip("/")), ASSETS)
        except (ValueError, KeyError, IndexError, OSError) as exc:
            self.json(400, {"error": str(exc)})

    def status(self, folder):
        status_path = folder / "status.json"
        state = read_json(status_path) if status_path.exists() else {"status": "saved"}
        output_name = state.get("output_dir", "outputs")
        if not isinstance(output_name, str) or not re.fullmatch(r"outputs(?:-[A-Za-z0-9_-]+)?", output_name):
            output_name = "outputs"
        output = folder / output_name
        manifest = output / "manifest.json"
        files = []
        # Only a reviewed export should be presented as complete.
        if manifest.exists():
            for p in sorted(output.rglob("*")):
                if p.is_file() and p.suffix.lower() in (".png", ".json", ".zip"):
                    files.append({"name": str(p.relative_to(output)), "url": f"/files/{folder.name}/{output_name}/{p.relative_to(output).as_posix()}"})
            state.setdefault("status", "exported")
        state["outputs"] = files
        state["background_url"] = f"/files/{folder.name}/{output_name}/background.png" if (output / "background.png").exists() else None
        state["preview_url"] = f"/files/{folder.name}/{output_name}/composite-preview.png" if (output / "composite-preview.png").exists() else None
        return state

    def do_POST(self):
        if not self.local_request():
            return
        if urlparse(self.path).path != "/api/projects":
            return self.json(404, {"error": "未知操作"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_BODY:
                return self.json(413, {"error": "项目太大，请缩小原图后重试"})
            payload = json.loads(self.rfile.read(size))
            return self.json(201, store_project(self.server.root, payload))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.json(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path, help="Projects and exports, outside the skill folder")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    args.root = args.root.expanduser().resolve()
    (args.root / "projects").mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.root = args.root
    info = {"url": f"http://127.0.0.1:{server.server_port}", "root": str(args.root), "pid": os.getpid()}
    write_json(args.root / "server-info.json", info)
    print(json.dumps(info), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
