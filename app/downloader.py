"""下载队列：串行下载（默认 1 个并发，保护 NAS 资源）、断点续传、限速、暂停/取消。"""
import asyncio
import time
import uuid
from pathlib import Path

import httpx

from . import db
from .cloud115 import cloud, CloudError
from .config import UA

_cancel_flags: dict[str, str] = {}  # task_id -> 'pause' | 'cancel'
_worker_task: asyncio.Task | None = None
CHUNK = 1024 * 256  # 256KB 一块，内存友好


def _speed_limit() -> int:
    """KB/s，0 表示不限速。"""
    try:
        return max(0, int(db.get_setting("speed_limit", "0")))
    except ValueError:
        return 0


def add_tasks(items: list[dict], target_dir: str) -> int:
    n = 0
    for it in items:
        # 同名任务（下载中/排队中）跳过
        dup = db.one("SELECT id FROM tasks WHERE pickcode=? AND status IN ('queued','downloading','paused')",
                     (it.get("pickcode"),))
        if dup:
            continue
        # rel：还原 115 目录结构（如 剧集名/Season 1），防目录穿越
        rel = (it.get("rel") or "").strip("/")
        if rel and ".." not in rel.split("/"):
            tdir = str(Path(target_dir) / rel)
        else:
            tdir = target_dir
        db.exe("""INSERT INTO tasks(id,name,pickcode,file_id,target_dir,size,status,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,'queued',?,?)""",
               (uuid.uuid4().hex[:12], it["name"], it.get("pickcode", ""),
                str(it.get("id", "")), tdir, it.get("size", 0),
                db.now(), db.now()))
        n += 1
    return n


def control(task_id: str, action: str):
    row = db.one("SELECT status FROM tasks WHERE id=?", (task_id,))
    if not row:
        return False
    if action == "pause" and row["status"] in ("queued", "downloading"):
        _cancel_flags[task_id] = "pause"
        db.exe("UPDATE tasks SET status='paused', updated_at=? WHERE id=? AND status='queued'",
               (db.now(), task_id))
        return True
    if action == "resume" and row["status"] in ("paused", "failed", "canceled"):
        _cancel_flags.pop(task_id, None)
        db.exe("UPDATE tasks SET status='queued', error='', updated_at=? WHERE id=?",
               (db.now(), task_id))
        return True
    if action == "cancel":
        _cancel_flags[task_id] = "cancel"
        db.exe("UPDATE tasks SET status='canceled', updated_at=? WHERE id=? AND status='queued'",
               (db.now(), task_id))
        return True
    if action == "delete" and row["status"] in ("done", "failed", "canceled"):
        db.exe("DELETE FROM tasks WHERE id=?", (task_id,))
        return True
    return False


async def start_worker():
    global _worker_task
    # 上次异常中断的任务复位为排队
    db.exe("UPDATE tasks SET status='queued' WHERE status='downloading'")
    _worker_task = asyncio.create_task(_loop())


async def _loop():
    while True:
        row = db.one("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1")
        if not row:
            await asyncio.sleep(2)
            continue
        try:
            await _download(row)
        except Exception as e:
            db.exe("UPDATE tasks SET status='failed', error=?, updated_at=? WHERE id=?",
                   (str(e)[:500], db.now(), row["id"]))
        await asyncio.sleep(0.5)


async def _download(task: dict):
    tid = task["id"]
    db.exe("UPDATE tasks SET status='downloading', error='', updated_at=? WHERE id=?",
           (db.now(), tid))

    if not cloud.is_logged_in():
        raise CloudError("115 未登录，请先在网页端扫码登录")
    url = await cloud.get_download_url(task["pickcode"])

    target_dir = Path(task["target_dir"])
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / task["name"]
    part = dest.with_name(dest.name + ".part")

    downloaded = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": UA}
    total = task["size"]
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120),
                                 follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as r:
            if r.status_code not in (200, 206):
                raise CloudError(f"下载请求失败 HTTP {r.status_code}")
            if r.status_code == 200:
                downloaded = 0  # 服务端不支持续传，重下
            cl = r.headers.get("content-length")
            if cl:
                total = downloaded + int(cl)
            limit = _speed_limit() * 1024
            window_start = time.monotonic()
            window_bytes = 0
            last_update = 0.0
            with open(part, "ab" if downloaded else "wb") as f:
                async for chunk in r.aiter_bytes(CHUNK):
                    flag = _cancel_flags.get(tid)
                    if flag:
                        _cancel_flags.pop(tid, None)
                        db.exe("UPDATE tasks SET status=?, downloaded=?, size=?, updated_at=? WHERE id=?",
                               ("canceled" if flag == "cancel" else "paused",
                                downloaded, total, db.now(), tid))
                        if flag == "cancel":
                            part.unlink(missing_ok=True)
                        return
                    f.write(chunk)
                    downloaded += len(chunk)
                    window_bytes += len(chunk)
                    # 限速
                    if limit:
                        elapsed = time.monotonic() - window_start
                        expected = window_bytes / limit
                        if expected > elapsed:
                            await asyncio.sleep(expected - elapsed)
                    # 每 0.8s 更新一次进度
                    now = time.monotonic()
                    if now - last_update > 0.8:
                        speed = int(window_bytes / max(now - window_start, 0.01))
                        db.exe("UPDATE tasks SET downloaded=?, size=?, speed=?, updated_at=? WHERE id=?",
                               (downloaded, total, speed, db.now(), tid))
                        last_update = now
                        window_start, window_bytes = now, 0

    part.rename(dest)
    db.exe("UPDATE tasks SET status='done', downloaded=?, size=?, speed=0, updated_at=? WHERE id=?",
           (downloaded, total, db.now(), tid))

    # 下载完成 → 若落在媒体库目录内，立即扫描该文件
    if db.get_setting("auto_scan", "1") == "1":
        from . import scanner
        for root, lib in ((scanner.movie_dir(), "movie"), (scanner.tv_dir(), "tv")):
            try:
                if str(dest).startswith(str(root)):
                    await scanner.scan_file(dest, lib)
                    break
            except Exception:
                pass
