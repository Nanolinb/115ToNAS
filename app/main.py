"""115 Media Hub — FastAPI 主程序与全部 API 路由。"""
import asyncio
import json
import mimetypes
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import auth, db, downloader, scanner, subtitles, tmdb_client
from .cloud115 import cloud, CloudError
from .config import MEDIA_ROOT, POSTER_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

VIDEO_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mov": "video/quicktime", ".ts": "video/mp2t", ".m2ts": "video/mp2t",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    await downloader.start_worker()
    yield


app = FastAPI(title="115 Media Hub", lifespan=lifespan)


# ---------------- 鉴权中间件 ----------------
# 观影端接口（默认免密，可选观影密码）；其余 /api 全部要管理端登录

PUBLIC_PREFIXES = ("/api/auth/",)
VIEWER_GET_EXACT = ("/api/library",)
VIEWER_PREFIXES = ("/api/poster/", "/api/stream/", "/api/subtitle/")
_VIEWER_MEDIA_RE = re.compile(r"^/api/media/\d+$")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    is_viewer = request.method == "GET" and (
        path in VIEWER_GET_EXACT
        or bool(_VIEWER_MEDIA_RE.match(path))
        or any(path.startswith(p) for p in VIEWER_PREFIXES)
    )
    try:
        if is_viewer:
            auth.require_viewer(request)
        else:
            auth.require_admin(request)
    except HTTPException as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


# ---------------- 静态页面 ----------------

@app.get("/", response_class=HTMLResponse)
async def index():
    """观影端：海报墙 + 播放，默认免密。"""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """管理端：115登录/下载/设置，独立登录通道。"""
    return FileResponse(STATIC_DIR / "admin.html")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------- 鉴权（管理端） ----------------

@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    pw = body.get("password", "")
    if not auth.is_configured():
        if len(pw) < 4:
            raise HTTPException(400, "密码至少 4 位")
        auth.set_password(pw)
    elif not auth.verify(pw):
        raise HTTPException(403, "密码错误")
    token = auth.create_session("admin")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {"configured": auth.is_configured(),
            "authed": auth.admin_authed(request),
            "viewer_password": auth.viewer_configured()}


@app.post("/api/auth/logout")
async def logout(request: Request):
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.post("/api/auth/password")
async def change_password(request: Request):
    body = await request.json()
    if not auth.verify(body.get("old", "")):
        raise HTTPException(403, "原密码错误")
    if len(body.get("new", "")) < 4:
        raise HTTPException(400, "新密码至少 4 位")
    auth.set_password(body["new"])
    return {"ok": True}


# ---------------- 鉴权（观影端） ----------------

@app.get("/api/auth/viewer_status")
async def viewer_status(request: Request):
    need = auth.viewer_configured()
    authed = True
    if need:
        try:
            auth.require_viewer(request)
        except HTTPException:
            authed = False
    return {"need_password": need, "authed": authed}


@app.post("/api/auth/viewer_login")
async def viewer_login(request: Request):
    body = await request.json()
    if not auth.viewer_configured():
        return {"ok": True}  # 未设观影密码，无需登录
    if not auth.verify_viewer(body.get("password", "")):
        raise HTTPException(403, "观影密码错误")
    token = auth.create_session("viewer")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/auth/viewer_password")
async def set_viewer_password(request: Request):
    """设置/清除观影密码（仅管理端）。"""
    auth.require_admin(request)
    body = await request.json()
    pw = body.get("password", "")
    if pw and len(pw) < 4:
        raise HTTPException(400, "观影密码至少 4 位；留空表示清除")
    auth.set_viewer_password(pw)
    return {"ok": True, "enabled": bool(pw)}


# ---------------- 媒体库 ----------------

def _entry_of_movie(row: dict) -> dict:
    return {
        "key": f"m{row['id']}", "kind": "movie", "id": row["id"],
        "title": row["title"], "name_cn": row["name_cn"],
        "year": row["year"], "poster": row["poster"], "rating": row["rating"],
        "genres": row["genres"] or "", "overview": row["overview"] or "",
        "tmdb_id": row["tmdb_id"], "size": row["size"], "has_sub": row["has_sub"],
    }


@app.get("/api/library")
async def library(q: str = "", type: str = "", year: int = 0, genre: str = ""):
    rows = db.q("SELECT * FROM media WHERE status='ok'")
    movies, shows = [], {}
    for r in rows:
        if r["type"] == "movie":
            movies.append(_entry_of_movie(r))
        else:
            gkey = f"t{r['tmdb_id']}" if r["tmdb_id"] else f"n:{r['title']}"
            show = shows.setdefault(gkey, {
                "key": gkey, "kind": "show", "title": r["title"],
                "name_cn": r["name_cn"], "year": r["year"], "poster": r["poster"],
                "rating": r["rating"], "genres": r["genres"] or "",
                "overview": r["overview"] or "", "tmdb_id": r["tmdb_id"],
                "episodes": [],
            })
            show["episodes"].append({
                "id": r["id"], "season": r["season"] or 1,
                "episode": r["episode"], "path": r["path"],
                "name": Path(r["path"]).name, "size": r["size"],
                "has_sub": r["has_sub"], "sub_status": r["sub_status"],
            })
            if not show["poster"] and r["poster"]:
                show["poster"] = r["poster"]

    entries = movies + list(shows.values())

    ql = q.strip().lower()
    if ql:
        entries = [e for e in entries if ql in (e["title"] or "").lower()
                   or ql in (e["name_cn"] or "").lower()]
    if type in ("movie", "show"):
        entries = [e for e in entries if e["kind"] == type]
    if year:
        entries = [e for e in entries if e["year"] == year]
    if genre:
        entries = [e for e in entries if genre in (e["genres"] or "")]

    for e in entries:
        if e["kind"] == "show":
            e["episodes"].sort(key=lambda x: (x["season"], x["episode"] or 0))
            e["count"] = len(e["episodes"])
    entries.sort(key=lambda e: ((e["title"] or "").lower()))
    return {"items": entries, "total": len(entries)}


@app.get("/api/media/{media_id}")
async def media_detail(media_id: int):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    row["filename"] = Path(row["path"]).name
    try:
        row["subs"] = json.loads(row.get("subs") or "[]")
    except ValueError:
        row["subs"] = []
    if not row["subs"] and row.get("sub_path"):
        row["subs"] = [{"lang": "zh", "label": "中文字幕", "path": row["sub_path"]}]
    for t in row["subs"]:
        t.setdefault("label", t.get("lang", "字幕"))
    return row


@app.get("/api/media/{media_id}/tracks")
async def media_tracks(media_id: int):
    """ffprobe 探测内嵌音轨/字幕数量（网页音轨菜单用）。"""
    row = db.one("SELECT path FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
            "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except FileNotFoundError:
        return {"available": False, "audio": 0, "subtitle": 0}
    except asyncio.TimeoutError:
        return {"available": False, "audio": 0, "subtitle": 0}
    types = out.decode(errors="ignore").split()
    return {"available": True,
            "audio": types.count("audio"),
            "subtitle": types.count("subtitle")}


@app.get("/api/media/{media_id}/tmdb_candidates")
async def tmdb_candidates(media_id: int, q: str = ""):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    kind = "tv" if row["type"] == "episode" else "movie"
    return {"items": await tmdb_client.search_candidates(q or row["title"], kind)}


@app.post("/api/media/{media_id}/rematch")
async def media_rematch(media_id: int, request: Request):
    body = await request.json()
    meta = await scanner.rematch(media_id, body.get("tmdb_id"),
                                 body.get("title"), body.get("year"))
    if not meta:
        raise HTTPException(404, "TMDB 未匹配到结果")
    return meta


@app.post("/api/media/{media_id}/subtitle")
async def media_subtitle(media_id: int):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row["path"])
    db.exe("UPDATE media SET sub_status='searching' WHERE id=?", (media_id,))
    tracks = subtitles.find_all_local_subs(path)
    have = {t["lang"] for t in tracks}
    for t in await subtitles.search_and_download(path, row["title"], row["year"]):
        if t["lang"] not in have:
            tracks.append(t)
            have.add(t["lang"])
    db.exe("""UPDATE media SET sub_status=?, has_sub=?, sub_path=?, subs=?
              WHERE id=?""",
           ("ok" if tracks else "failed", 1 if tracks else 0,
            tracks[0]["path"] if tracks else None,
            json.dumps(tracks, ensure_ascii=False), media_id))
    return {"found": bool(tracks), "tracks": tracks}


@app.delete("/api/media/{media_id}")
async def media_remove(media_id: int):
    db.exe("DELETE FROM media WHERE id=?", (media_id,))
    return {"ok": True}


# ---------------- 海报 / 流媒体 / 字幕 ----------------

_PLACEHOLDER = (b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450">'
                b'<rect width="300" height="450" fill="#1c2230"/>'
                b'<text x="150" y="230" fill="#3a4557" font-size="60" '
                b'text-anchor="middle" font-family="sans-serif">&#127916;</text></svg>')


@app.get("/api/poster/{fname}")
async def poster(fname: str):
    fname = Path(fname).name  # 防目录穿越
    p = POSTER_DIR / fname
    if p.exists():
        return FileResponse(p, headers={"Cache-Control": "max-age=86400"})
    return Response(_PLACEHOLDER, media_type="image/svg+xml")


@app.get("/api/stream/{media_id}")
async def stream(media_id: int, request: Request):
    row = db.one("SELECT path FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(404, "文件不存在")

    size = path.stat().st_size
    start, end = 0, size - 1
    status = 200
    range_header = request.headers.get("range", "")
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if m:
        if m.group(1):
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
        elif m.group(2):
            start = max(0, size - int(m.group(2)))
        status = 206
    length = end - start + 1

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    mime = VIDEO_MIME.get(path.suffix.lower()) or \
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f"inline; filename*=UTF-8''{__import__('urllib.parse', fromlist=['quote']).quote(path.name)}",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(gen(), status_code=status, headers=headers,
                             media_type=mime)


@app.get("/api/subtitle/{media_id}")
async def subtitle(media_id: int):
    return await subtitle_track(media_id, 0)


@app.get("/api/subtitle/{media_id}/{idx}")
async def subtitle_track(media_id: int, idx: int):
    row = db.one("SELECT subs, sub_path FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "无字幕")
    try:
        tracks = json.loads(row["subs"]) if row["subs"] else []
    except ValueError:
        tracks = []
    if not tracks and row["sub_path"]:
        tracks = [{"lang": "zh", "path": row["sub_path"]}]
    if idx < 0 or idx >= len(tracks):
        raise HTTPException(404, "无字幕")
    p = Path(tracks[idx]["path"])
    if not p.exists():
        raise HTTPException(404, "字幕文件不存在")
    text = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() == ".srt":
        vtt = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})",
                                    r"\1.\2", text)
        return Response(vtt, media_type="text/vtt; charset=utf-8")
    # ass/ssa 浏览器无法渲染，原样返回供下载
    return Response(text, media_type="text/plain; charset=utf-8")


# ---------------- 扫描 ----------------

@app.post("/api/scan")
async def trigger_scan():
    if not scanner.scan_state["running"]:
        asyncio.create_task(scanner.scan_all())
    return {"ok": True}


@app.get("/api/scan/status")
async def scan_status():
    return scanner.scan_state


# ---------------- 115 网盘 ----------------

@app.get("/api/cloud/status")
async def cloud_status():
    return {"logged_in": await asyncio.to_thread(cloud.is_logged_in)}


@app.post("/api/cloud/logout")
async def cloud_logout():
    cloud.logout()
    return {"ok": True}


@app.post("/api/cloud/qrcode/new")
async def cloud_qr_new():
    try:
        data = await cloud.new_qrcode()
    except CloudError as e:
        raise HTTPException(502, str(e))
    return {"uid": data["uid"]}


@app.get("/api/cloud/qrcode/{uid}.png")
async def cloud_qr_png(uid: str):
    png = cloud.qr_png(uid)
    if not png:
        raise HTTPException(404, "二维码已过期，请重新获取")
    return Response(png, media_type="image/png")


@app.get("/api/cloud/qrcode/{uid}/status")
async def cloud_qr_status(uid: str):
    try:
        return await cloud.poll_qrcode(uid)
    except CloudError as e:
        raise HTTPException(502, str(e))


@app.get("/api/cloud/list")
async def cloud_list(cid: str = "0", offset: int = 0, sort: str = "time_desc"):
    try:
        return await cloud.list_files(cid, offset, sort=sort)
    except CloudError as e:
        raise HTTPException(502, str(e))


@app.get("/api/cloud/search")
async def cloud_search(q: str):
    if not q.strip():
        return {"items": []}
    try:
        return {"items": await cloud.search(q.strip())}
    except CloudError as e:
        raise HTTPException(502, str(e))


@app.post("/api/cloud/download")
async def cloud_download(request: Request):
    body = await request.json()
    items = body.get("items") or []
    target = body.get("target_dir") or db.get_setting(
        "download_dir", str(MEDIA_ROOT / "downloads"))
    if not items:
        raise HTTPException(400, "未选择文件")

    async def expand_and_enqueue():
        from .parser import is_video
        flat = []
        for it in items:
            if it.get("is_dir"):
                try:
                    async for v in cloud.iter_video_files(it["id"]):
                        flat.append(v)
                except CloudError:
                    pass
            elif is_video(it["name"]):
                flat.append(it)
        return downloader.add_tasks(flat, target)

    n = await expand_and_enqueue()
    return {"queued": n, "target_dir": target}


# ---------------- 下载任务 ----------------

@app.get("/api/tasks")
async def tasks():
    rows = db.q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200")
    return {"items": rows}


@app.post("/api/tasks/{task_id}/{action}")
async def task_control(task_id: str, action: str):
    if action not in ("pause", "resume", "cancel", "delete"):
        raise HTTPException(400, "未知操作")
    if not downloader.control(task_id, action):
        raise HTTPException(400, "当前状态不允许该操作")
    return {"ok": True}


# ---------------- 设置与目录管理 ----------------

SETTING_KEYS = ("movie_dir", "tv_dir", "download_dir", "tmdb_key",
                "assrt_token", "speed_limit", "auto_scan", "proxy_url")
SECRET_SETTING_KEYS = ("tmdb_key", "assrt_token")
MASK = "****"


def _mask_secret(v: str) -> str:
    return (MASK + v[-4:]) if v else ""


@app.get("/api/settings")
async def get_settings():
    from .config import MEDIA_ROOT as MR
    defaults = {
        "movie_dir": str(MR / "movies"),
        "tv_dir": str(MR / "tv"),
        "download_dir": str(MR / "downloads"),
        "tmdb_key": "", "assrt_token": "",
        "speed_limit": "0", "auto_scan": "1", "proxy_url": "",
    }
    out = {}
    for k, dft in defaults.items():
        if k in SECRET_SETTING_KEYS:
            out[k] = _mask_secret(db.get_secret(k, dft))
        else:
            out[k] = db.get_setting(k, dft)
    return out


@app.post("/api/settings")
async def save_settings(request: Request):
    body = await request.json()
    for k in SETTING_KEYS:
        if k not in body:
            continue
        v = str(body[k]).strip()
        if k in SECRET_SETTING_KEYS:
            if v.startswith(MASK):
                continue  # 掩码原样回传 = 未修改
            db.set_secret(k, v)
        else:
            db.set_setting(k, v)
    return {"ok": True}


def _safe_under_root(path: str) -> Path:
    p = Path(path).resolve()
    root = MEDIA_ROOT.resolve()
    if p != root and root not in p.parents:
        raise HTTPException(403, "只能访问媒体挂载目录内的路径")
    return p


@app.get("/api/fs/list")
async def fs_list(path: str = ""):
    p = _safe_under_root(path) if path else MEDIA_ROOT.resolve()
    dirs = []
    try:
        for entry in sorted(p.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({"name": entry.name, "path": str(entry)})
    except OSError as e:
        raise HTTPException(400, str(e))
    parent = str(p.parent) if p != MEDIA_ROOT.resolve() else None
    return {"path": str(p), "dirs": dirs, "parent": parent,
            "root": str(MEDIA_ROOT.resolve())}


@app.post("/api/fs/mkdir")
async def fs_mkdir(request: Request):
    body = await request.json()
    p = _safe_under_root(body.get("path", ""))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "path": str(p)}
