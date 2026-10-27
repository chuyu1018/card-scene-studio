#!/usr/bin/env python3
"""One-shot local handoff to an active Codex turn; never executes generation."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
import uuid


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@contextmanager
def _claim_lock(folder):
    """Serialize brief listener changes; OS releases the lock after a crash."""
    with (folder / "listener.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise ValueError("排版等待状态正在更新，请稍后重试") from exc
        try:
            yield
        finally:
            release()


def _live(root):
    data = _read(Path(root) / ".handoff" / "listener.json")
    if not isinstance(data, dict):
        return None
    ident, pid, deadline = data.get("listener_id"), data.get("pid"), data.get("expires_at")
    if (not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident)
            or type(pid) is not int or pid <= 0
            or type(deadline) not in (int, float) or not time.time() < deadline):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return data


def listener_state(root):
    data = _live(root)
    if not data:
        return {"active": False, "listener_id": None, "expires_at": None}
    pending = (Path(root) / ".handoff" / (data["listener_id"] + ".json")).exists()
    return {"active": not pending, "listener_id": data["listener_id"],
            "expires_at": datetime.fromtimestamp(data["expires_at"], timezone.utc).isoformat()}


def enqueue(root, listener_id, project_path):
    """Accept only the first submission for a live, matching listener."""
    root = Path(root).resolve()
    data = _live(root)
    if not data or listener_id != data["listener_id"]:
        return False
    project_path = Path(project_path).resolve()
    projects = root / "projects"
    if (project_path.name != "project.json" or project_path.parent.parent != projects
            or not project_path.is_file()):
        raise ValueError("交接项目必须保存在当前输出目录内")
    target = root / ".handoff" / (listener_id + ".json")
    request = {"listener_id": listener_id, "project_path": str(project_path), "submitted_at": time.time()}
    try:
        with target.open("x", encoding="utf-8") as stream:
            json.dump(request, stream, ensure_ascii=False)
    except FileExistsError:
        return False
    # Do not report delivery if the listener expired during the submission.
    receipt = target.with_suffix(".received")
    until = time.monotonic() + 1
    while time.monotonic() < until:
        if receipt.is_file():
            return True
        time.sleep(0.025)
    return False


def wait_for_project(root, timeout=600, ready=None):
    """Block for one explicit submit, up to timeout seconds; return a trusted path."""
    root = Path(root).resolve()
    folder = root / ".handoff"
    folder.mkdir(parents=True, exist_ok=True)
    listener = folder / "listener.json"
    ident = uuid.uuid4().hex
    data = {"listener_id": ident, "pid": os.getpid(), "expires_at": time.time() + timeout}
    with _claim_lock(folder):
        if _live(root):
            raise ValueError("这个输出目录已有 Codex 正在等待排版，请复用该页面")
        temporary = folder / (ident + ".listener.tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(listener)
    request_file = folder / (ident + ".json")
    try:
        if ready:
            ready(data)
        while time.time() < data["expires_at"]:
            request = _read(request_file)
            if isinstance(request, dict) and request.get("listener_id") == ident:
                path_value = request.get("project_path")
                if not isinstance(path_value, str):
                    raise ValueError("交接项目路径不正确")
                path = Path(path_value).resolve()
                if path.name != "project.json" or path.parent.parent != root / "projects" or not path.is_file():
                    raise ValueError("交接项目不存在或不属于当前输出目录")
                request_file.with_suffix(".received").write_text(str(path), encoding="utf-8")
                return {"status": "submitted", "project_path": str(path), "listener_id": ident}
            time.sleep(min(0.25, max(0.01, data["expires_at"] - time.time())))
        return {"status": "timeout"}
    finally:
        try:
            with _claim_lock(folder):
                current = _read(listener)
                if isinstance(current, dict) and current.get("listener_id") == ident:
                    listener.unlink(missing_ok=True)
        except ValueError:
            pass  # An expired marker can be replaced by the next listener.


def main():
    parser = argparse.ArgumentParser(description="Wait once for the local editor's submit button")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        parser.error("timeout must be between 1 and 1800 seconds")
    def ready(data):
        print(json.dumps({"status": "waiting", **data}), flush=True)
    try:
        result = wait_for_project(args.root, args.timeout, ready)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
