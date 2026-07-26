"""115 Media Hub — FastAPI 主程序与全部 API 路由。"""
import asyncio
import json
import mimetypes
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import auth, baiduimg, db, downloader, scanner, subtitles, tmdb_client
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
_VIEWER_PLAYLINK_RE = re.compile(r"^/api/media/\d+/playlink$")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    is_viewer = request.method == "GET" and (
        path in VIEWER_GET_EXACT
        or bool(_VIEWER_MEDIA_RE.match(path))
        or bool(_VIEWER_PLAYLINK_RE.match(path))
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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """基础安全响应头：防 MIME 嗅探 / 防点击劫持 / 不泄露来源。"""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


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
    client_ip = request.client.host if request.client else "unknown"
    locked = auth.rate_locked(f"login:{client_ip}")
    if locked:
        raise HTTPException(429, f"失败次数过多，请 {locked // 60 + 1} 分钟后再试")
    body = await request.json()
    pw = body.get("password", "")
    if not auth.is_configured():
        if len(pw) < 4:
            raise HTTPException(400, "密码至少 4 位")
        auth.set_password(pw)
    elif not auth.verify(pw):
        auth.rate_record_fail(f"login:{client_ip}")
        raise HTTPException(403, "密码错误")
    auth.rate_clear(f"login:{client_ip}")
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
    client_ip = request.client.host if request.client else "unknown"
    locked = auth.rate_locked(f"pw:{client_ip}")
    if locked:
        raise HTTPException(429, f"失败次数过多，请 {locked // 60 + 1} 分钟后再试")
    body = await request.json()
    if not auth.verify(body.get("old", "")):
        auth.rate_record_fail(f"pw:{client_ip}")
        raise HTTPException(403, "原密码错误")
    auth.rate_clear(f"pw:{client_ip}")
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
    client_ip = request.client.host if request.client else "unknown"
    locked = auth.rate_locked(f"vlogin:{client_ip}")
    if locked:
        raise HTTPException(429, f"失败次数过多，请 {locked // 60 + 1} 分钟后再试")
    if not auth.verify_viewer(body.get("password", "")):
        auth.rate_record_fail(f"vlogin:{client_ip}")
        raise HTTPException(403, "观影密码错误")
    auth.rate_clear(f"vlogin:{client_ip}")
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
    # 缺简介的条目后台补（subhd/豆瓣），下轮刷新即见，不拖慢首屏
    missing = [r["id"] for r in rows if not (r["overview"] or "").strip()]
    if missing:
        from . import overview as _ov
        _ov.schedule_fill(missing[:20])
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
    """ffprobe 探测内嵌音轨/字幕数量与音频编码（网页音轨菜单与无声提示用）。"""
    row = db.one("SELECT path FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
            "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except FileNotFoundError:
        return {"available": False, "audio": 0, "subtitle": 0, "audio_codecs": []}
    except asyncio.TimeoutError:
        return {"available": False, "audio": 0, "subtitle": 0, "audio_codecs": []}
    audio_codecs, sub_n = [], 0
    for line in out.decode(errors="ignore").splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if "audio" in parts:
            codec = next((p for p in parts if p != "audio"), "")
            audio_codecs.append(codec)
        elif "subtitle" in parts:
            sub_n += 1
    return {"available": True, "audio": len(audio_codecs),
            "subtitle": sub_n, "audio_codecs": audio_codecs}


@app.get("/api/media/{media_id}/playlink")
async def media_playlink(media_id: int):
    """签名播放链接：IINA/VLC/电视等外部播放器无 Cookie，用 ?pt= 令牌鉴权，24 小时有效。"""
    row = db.one("SELECT id FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    token = auth.make_play_token(media_id)
    return {"url": f"/api/stream/{media_id}?pt={token}",
            "expires_in": auth.PLAY_LINK_TTL}


@app.get("/api/media/{media_id}/tmdb_candidates")
async def tmdb_candidates(media_id: int, q: str = ""):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    kind = "tv" if row["type"] == "episode" else "movie"
    # 默认搜索词：从文件名解析关键信息（比库里的旧匹配标题更可靠）
    from . import parser as _parser
    info = _parser.parse(Path(row["path"]).name)
    return {"items": await tmdb_client.search_candidates(
        q or info["title"] or row["title"], kind)}


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


# ---------------- 封面（subhd/豆瓣/百度三源 + 本地上传） ----------------

_POSTER_CAND_CACHE: dict = {}  # (media_id, kw) -> (ts, items)，预取结果缓存 10 分钟


async def _gather_posters(kw: str, want: int) -> list:
    """按源顺序收集封面候选。want>0 为预取模式（只要 subhd+豆瓣，够数即停，求快）；
    want<=0 为全量（subhd 6 + 豆瓣 6 + 百度 12）。"""
    from . import douban, subhd
    items = []
    for u in await subhd.search_posters(kw, want if want > 0 else 6):
        items.append({"display": u, "url": u, "source": "subhd"})
    if want <= 0 or len(items) < want:
        for u in await douban.search_posters(kw, (want - len(items)) if want > 0 else 6):
            items.append({"display": f"/api/imgproxy?u={quote(u)}",
                          "url": u, "source": "douban"})
    if want <= 0:
        for u in await baiduimg.search_posters(f"{kw} 海报", 12):
            items.append({"display": u, "url": u, "source": "baidu"})
    return items[:want] if want > 0 else items


@app.get("/api/media/{media_id}/poster_candidates")
async def poster_candidates(media_id: int, q: str = "", limit: int = 0):
    """封面候选墙。limit>0：快速预取（结果缓存 10 分钟，详情弹窗打开时后台调用，
    让「更换封面」点开即有候选）；limit=0：全量三源搜索（「更多图片」按钮）。
    默认搜索词从文件名解析关键信息；豆瓣图有防盗链，显示走 /api/imgproxy。"""
    row = db.one("SELECT title, name_cn, year, path FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    from . import parser as _parser
    info = _parser.parse(Path(row["path"]).name)
    base = row["name_cn"] or info["title"] or row["title"]
    yr = row["year"] or info["year"] or ""
    kw = q.strip() or f"{base} {yr}".strip()
    if limit > 0:
        key = (media_id, kw)
        hit = _POSTER_CAND_CACHE.get(key)
        if hit and time.time() - hit[0] < 600:
            return {"items": hit[1], "query": kw}
        items = await _gather_posters(kw, min(limit, 10))
        if len(_POSTER_CAND_CACHE) > 200:
            _POSTER_CAND_CACHE.clear()
        _POSTER_CAND_CACHE[key] = (time.time(), items)
        return {"items": items, "query": kw}
    return {"items": await _gather_posters(kw, 0), "query": kw}


@app.get("/api/imgproxy")
async def imgproxy(u: str):
    """防盗链图床的显示代理（仅白名单图床，防 SSRF 滥用）。"""
    host = httpx_host(u)
    if not (host.endswith(".doubanio.com") or host.endswith(".subhd.me")):
        raise HTTPException(403, "不允许的图片来源")
    got = await baiduimg.fetch_image(u)
    if not got:
        raise HTTPException(502, "图片抓取失败")
    data, ctype = got
    return Response(data, media_type=ctype,
                    headers={"Cache-Control": "max-age=86400"})


def httpx_host(url: str) -> str:
    import httpx as _h
    return (_h.URL(url).host or "").lower()


_IMG_MAGIC = ((b"\xff\xd8", ".jpg"), (b"\x89PNG", ".png"),
              (b"GIF8", ".gif"), (b"RIFF", ".webp"))


@app.post("/api/media/{media_id}/poster_upload")
async def poster_upload(media_id: int, request: Request):
    """本地上传封面：请求体即图片字节（免 multipart 依赖），
    魔数校验格式，剧集同剧共享。"""
    row = db.one("SELECT id, type, tmdb_id FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    data = await request.body()
    if len(data) < 500:
        raise HTTPException(400, "不是有效的图片")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 15MB")
    ext = next((e for magic, e in _IMG_MAGIC if data.startswith(magic)), None)
    if not ext:
        raise HTTPException(400, "仅支持 JPG / PNG / GIF / WEBP")
    fname = f"up_{media_id}_{int(time.time())}{ext}"
    (POSTER_DIR / fname).write_bytes(data)
    db.exe("UPDATE media SET poster=? WHERE id=?", (fname, media_id))
    # 剧集：同剧其它集共享同一封面
    if row["type"] == "episode" and row["tmdb_id"]:
        db.exe("UPDATE media SET poster=? WHERE tmdb_id=?",
               (fname, row["tmdb_id"]))
    return {"ok": True, "poster": fname}


@app.post("/api/media/{media_id}/poster")
async def set_poster(media_id: int, request: Request):
    row = db.one("SELECT id, type, tmdb_id FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "无效的图片地址")
    fname = await baiduimg.download_poster(url)
    if not fname:
        raise HTTPException(502, "封面下载失败，换一张试试")
    db.exe("UPDATE media SET poster=? WHERE id=?", (fname, media_id))
    # 剧集：同剧其它集共享同一封面
    if row["type"] == "episode" and row["tmdb_id"]:
        db.exe("UPDATE media SET poster=? WHERE tmdb_id=?",
               (fname, row["tmdb_id"]))
    return {"ok": True, "poster": fname}


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


# 字幕语言中缀：Movie.zh.srt / Movie.en.ass / Movie.zh-en.srt 等
_LANG_INFIX_RE = re.compile(r"\.(zh(?:-en)?|en|eng|chs|cht|zho|chi|sc|tc)$", re.IGNORECASE)
_BAD_DIR_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_dirname(name: str) -> str:
    cleaned = _BAD_DIR_CHARS.sub(" ", name)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" .") or "未命名"


def _pair_subs(batch: list, drop_unmatched: bool = False) -> list:
    """字幕与视频按（相对目录 + 文件名主干）配对；
    配对成功的字幕改名为 <视频主干><语言中缀>.<扩展名>，保证扫描器能识别。"""
    from .parser import is_video
    vids = {(b.get("rel", ""), Path(b["name"]).stem.lower()): b
            for b in batch if is_video(b["name"])}
    out = [b for b in batch if is_video(b["name"])]
    for b in batch:
        if is_video(b["name"]):
            continue
        stem = Path(b["name"]).stem
        ext = Path(b["name"]).suffix
        base = _LANG_INFIX_RE.sub("", stem)
        v = None
        for cand in (stem.lower(), base.lower()):
            v = vids.get((b.get("rel", ""), cand))
            if v:
                break
        if v:
            infix = stem[len(base):]  # ".zh" / ".en" 等，可能为空
            out.append({**b, "name": f"{Path(v['name']).stem}{infix}{ext}"})
        elif not drop_unmatched:
            out.append(b)
    return out


@app.post("/api/cloud/download")
async def cloud_download(request: Request):
    body = await request.json()
    items = body.get("items") or []
    target = body.get("target_dir") or db.get_setting(
        "download_dir", str(MEDIA_ROOT / "downloads"))
    if not items:
        raise HTTPException(400, "未选择文件")

    async def expand_and_enqueue():
        from .parser import is_subtitle, is_video, parse
        added = skipped = 0
        for it in items:
            if it.get("is_dir"):
                # 整文件夹：视频 + 同目录字幕，还原 115 目录结构
                try:
                    batch = [v async for v in
                             cloud.iter_media_files(it["id"], _safe_dirname(it["name"]))]
                except CloudError:
                    continue
                a, s = downloader.add_tasks(_pair_subs(batch), target)
            elif is_video(it["name"]):
                # 单选视频：剧集自动按剧名建文件夹；顺带拉同目录同名字幕
                info = parse(it["name"])
                rel = _safe_dirname(info["title"]) if info["episode"] is not None else ""
                batch = [{**it, "rel": rel}]
                pcid = it.get("parent_cid")
                if pcid:
                    try:
                        batch += [{**s, "rel": rel}
                                  for s in await cloud.list_sibling_subs(pcid)]
                    except CloudError:
                        pass
                a, s = downloader.add_tasks(_pair_subs(batch, drop_unmatched=True), target)
            elif is_subtitle(it["name"]):
                # 单选字幕：直接落到目标目录根，配对交给扫描器（严格+模糊）
                a, s = downloader.add_tasks([{**it, "rel": ""}], target)
            else:
                continue
            added += a
            skipped += s
        return added, skipped

    added, skipped = await expand_and_enqueue()
    return {"queued": added, "skipped": skipped, "target_dir": target}


# ---------------- 下载任务 ----------------

@app.get("/api/tasks")
async def tasks():
    rows = db.q("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200")
    return {"items": rows}


@app.post("/api/tasks/batch")
async def tasks_batch(request: Request):
    """批量操作：pause_all 全部暂停 / resume_all 全部启动 / clear_done 清空已下载 /
    retarget 改存储位置（ids + target_dir）/ delete 删记录与临时文件（ids）。"""
    body = await request.json()
    action = body.get("action", "")
    skipped = 0
    if action == "pause_all":
        n = downloader.pause_all()
    elif action == "resume_all":
        n = downloader.resume_all()
    elif action == "clear_done":
        n = downloader.clear_done()
    elif action == "retarget":
        ids = [str(i) for i in (body.get("ids") or [])]
        target = (body.get("target_dir") or "").strip()
        if not ids or not target:
            raise HTTPException(400, "缺少任务或目标目录")
        _safe_under_root(target)  # 目标目录必须在媒体挂载内
        n, skipped = downloader.retarget_tasks(ids, target)
    elif action == "delete":
        ids = [str(i) for i in (body.get("ids") or [])]
        if not ids:
            raise HTTPException(400, "未选择任务")
        n, skipped = downloader.delete_tasks(ids)
    else:
        raise HTTPException(400, "未知操作")
    return {"ok": True, "affected": n, "skipped": skipped}


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
    # TMDB 连通状态（只读，刮削时自动更新）：direct / proxy / fail
    out["tmdb_net"] = db.get_setting("tmdb_net", "")
    out["tmdb_net_at"] = db.get_setting("tmdb_net_at", "")
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
async def fs_list(path: str = "", with_files: bool = False):
    p = _safe_under_root(path) if path else MEDIA_ROOT.resolve()
    dirs, files = [], []
    try:
        for entry in sorted(p.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
            elif with_files and entry.is_file():
                try:
                    st = entry.stat()
                except OSError:
                    continue
                files.append({"name": entry.name, "path": str(entry),
                              "size": st.st_size, "mtime": int(st.st_mtime)})
    except OSError as e:
        raise HTTPException(400, str(e))
    parent = str(p.parent) if p != MEDIA_ROOT.resolve() else None
    return {"path": str(p), "dirs": dirs, "files": files, "parent": parent,
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


# ---------------- 文件管理（管理端） ----------------

def _sync_db_path(src: Path, dst: Path | None, is_dir: bool):
    """文件被删除/移动后同步媒体库与下载历史，避免观影端出现失效条目。
    dst=None 表示删除；目录按路径前缀整体平移。is_dir 须在文件系统操作前取好。"""
    if dst is None:
        if is_dir:
            db.exe("DELETE FROM media WHERE path LIKE ?", (str(src) + "/%",))
            db.exe("DELETE FROM downloads WHERE path LIKE ?", (str(src) + "/%",))
        else:
            db.exe("DELETE FROM media WHERE path=?", (str(src),))
            db.exe("DELETE FROM downloads WHERE path=?", (str(src),))
        return
    if is_dir:
        # SQLite substr 从 1 开始：去掉 "src/" 前缀（长 len(src)+1）后拼 "dst/" 新前缀
        for table in ("media", "downloads"):
            db.exe(f"UPDATE {table} SET path = ? || '/' || substr(path, ?) WHERE path LIKE ?",
                   (str(dst), len(str(src)) + 2, str(src) + "/%"))
    else:
        db.exe("UPDATE media SET path=? WHERE path=?", (str(dst), str(src)))
        db.exe("UPDATE downloads SET path=? WHERE path=?", (str(dst), str(src)))


def _check_not_root(p: Path):
    if p == MEDIA_ROOT.resolve():
        raise HTTPException(403, "不能对媒体根目录本身操作")


@app.post("/api/fs/delete")
async def fs_delete(request: Request):
    body = await request.json()
    p = _safe_under_root(body.get("path", ""))
    _check_not_root(p)
    if not p.exists():
        raise HTTPException(404, "路径不存在")
    is_dir = p.is_dir()
    try:
        if is_dir:
            shutil.rmtree(p)
        else:
            p.unlink()
    except OSError as e:
        raise HTTPException(400, str(e))
    _sync_db_path(p, None, is_dir)
    return {"ok": True}


@app.post("/api/fs/move")
async def fs_move(request: Request):
    body = await request.json()
    src = _safe_under_root(body.get("src", ""))
    dst_dir = _safe_under_root(body.get("dst_dir", ""))
    _check_not_root(src)
    if not src.exists():
        raise HTTPException(404, "源路径不存在")
    is_dir = src.is_dir()
    if not dst_dir.is_dir():
        raise HTTPException(400, "目标不是目录")
    if is_dir and (dst_dir == src or src in dst_dir.parents):
        raise HTTPException(400, "不能移动到自身内部")
    dst = dst_dir / src.name
    if dst.exists():
        raise HTTPException(400, "目标位置已存在同名文件或文件夹")
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        raise HTTPException(400, str(e))
    _sync_db_path(src, dst, is_dir)
    return {"ok": True, "path": str(dst)}


@app.post("/api/fs/rename")
async def fs_rename(request: Request):
    body = await request.json()
    src = _safe_under_root(body.get("path", ""))
    _check_not_root(src)
    name = (body.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "无效的名称")
    if not src.exists():
        raise HTTPException(404, "路径不存在")
    is_dir = src.is_dir()
    dst = src.parent / name
    if dst.exists():
        raise HTTPException(400, "已存在同名文件或文件夹")
    try:
        src.rename(dst)
    except OSError as e:
        raise HTTPException(400, str(e))
    _sync_db_path(src, dst, is_dir)
    return {"ok": True, "path": str(dst)}
