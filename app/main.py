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
VIEWER_PREFIXES = ("/api/poster/", "/api/stream/", "/api/subtitle/",
                   "/api/stream-prep/")
_VIEWER_MEDIA_RE = re.compile(r"^/api/media/\d+$")
_VIEWER_PLAYLINK_RE = re.compile(r"^/api/media/\d+/playlink$")
_VIEWER_TRACKS_RE = re.compile(r"^/api/media/\d+/tracks$")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    is_viewer = request.method == "GET" and (
        path in VIEWER_GET_EXACT
        or bool(_VIEWER_MEDIA_RE.match(path))
        or bool(_VIEWER_PLAYLINK_RE.match(path))
        or bool(_VIEWER_TRACKS_RE.match(path))
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
    """观影端：海报墙 + 播放，默认免密。HTML 不缓存（静态资源带版本号）。"""
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """管理端：115登录/下载/设置，独立登录通道。"""
    return FileResponse(STATIC_DIR / "admin.html",
                        headers={"Cache-Control": "no-cache"})

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
        "backdrop": row.get("backdrop"),
        "genres": row["genres"] or "", "overview": row["overview"] or "",
        "tmdb_id": row["tmdb_id"], "size": row["size"], "has_sub": row["has_sub"],
    }


@app.get("/api/library")
async def library(q: str = "", type: str = "", year: int = 0, genre: str = ""):
    rows = db.q("SELECT * FROM media WHERE status='ok'")
    # 防御：字幕等非视频路径的历史脏数据不上墙（scan_file 已拒绝入库）
    from .parser import is_video
    rows = [r for r in rows if is_video(r["path"])]
    # 现实校验：文件已被搬走/删除（不经本应用），或指向不在指定库目录内
    # → 标 missing 立即下墙。库目录挂载不可达时跳过该校验，防止误判
    from . import scanner as _sc
    roots_cfg = [p.resolve() for p in (_sc.movie_dir(), _sc.tv_dir())]
    roots_up = [p for p in roots_cfg if p.exists()]
    stale = []
    for r in rows:
        p = Path(r["path"])
        under = [root for root in roots_cfg if p == root or root in p.parents]
        if not under or (any(root in roots_up for root in under)
                         and not p.exists()):
            stale.append(r["id"])
    if stale:
        db.exe(f"UPDATE media SET status='missing' "
               f"WHERE id IN ({','.join('?' * len(stale))})", stale)
        gone = set(stale)
        rows = [r for r in rows if r["id"] not in gone]
    movies, shows = [], {}
    for r in rows:
        if r["type"] == "movie":
            movies.append(_entry_of_movie(r))
        else:
            # 按 剧+季 分组（季号来自文件名解析），同剧不同季各自成卡
            season = r["season"] or 1
            base = f"t{r['tmdb_id']}" if r["tmdb_id"] else f"n:{r['title']}"
            show = shows.setdefault(f"{base}:s{season}", {
                "key": f"{base}:s{season}", "kind": "show", "base": base,
                "season": season, "title": r["title"],
                "name_cn": r["name_cn"], "year": r["year"], "poster": r["poster"],
                "backdrop": r.get("backdrop"),
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
            if not show["backdrop"] and r.get("backdrop"):
                show["backdrop"] = r["backdrop"]

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

    # 季标：一律标注第N季（季号来自文件名解析的 Sxx）
    for e in entries:
        if e["kind"] == "show":
            suf = f" 第{e['season']}季"
            e["title"] = (e["title"] or "") + suf
            if e["name_cn"]:
                e["name_cn"] += suf
    for e in entries:
        if e["kind"] == "show":
            e["episodes"].sort(key=lambda x: (x["season"], x["episode"] or 0))
            e["count"] = len(e["episodes"])
    entries.sort(key=lambda e: ((e["title"] or "").lower()))
    # 缺简介/题材/年份的条目后台补（subhd/豆瓣），下轮刷新即见，不拖慢首屏
    missing = [r["id"] for r in rows
               if not (r["overview"] or "").strip()
               or not (r["genres"] or "").strip() or not r["year"]]
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
        stored = json.loads(row.get("subs") or "[]")
    except ValueError:
        stored = []
    if not stored and row.get("sub_path"):
        stored = [{"lang": "zh", "label": "中文字幕", "path": row["sub_path"]}]
    subs = stored
    vp = Path(row["path"])
    if vp.exists():
        # 播放时实时重扫同目录字幕：扫描后才放进文件夹的字幕（模糊命名也算）
        # 也能自动挂载、在播放器里切换，不必手动重扫
        subs = subtitles.find_all_local_subs(vp)
        known = {t["path"] for t in subs}
        for t in stored:  # 在线下载等历史轨道：文件还在就保留
            if t.get("path") not in known and Path(t.get("path") or "").exists():
                subs.append(t)
                known.add(t["path"])
        subs.sort(key=lambda t: subtitles.LANG_ORDER.get(t.get("lang"), 9))
        key = [(t.get("lang"), t.get("path")) for t in subs]
        if key != [(t.get("lang"), t.get("path")) for t in stored]:
            db.exe("UPDATE media SET has_sub=?, sub_path=?, subs=? WHERE id=?",
                   (1 if subs else 0, subs[0]["path"] if subs else None,
                    json.dumps(subs, ensure_ascii=False), media_id))
    for t in subs:
        t.setdefault("label", t.get("lang", "字幕"))
    row["subs"] = subs
    return row


@app.get("/api/media/{media_id}/tracks")
async def media_tracks(media_id: int):
    """ffprobe 探测内嵌音轨/字幕：数量、音频编码、语言标签（网页音轨菜单用）。"""
    row = db.one("SELECT path FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    data = await _probe_streams(path)
    if data is None:
        return {"available": False, "audio": 0, "subtitle": 0,
                "audio_codecs": [], "audio_tracks": [], "preferred_audio": 0,
                "duration": 0, "needs_transcode": False}
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    audio_codecs, audio_tracks, sub_n = [], [], 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "audio":
            audio_codecs.append(s.get("codec_name") or "")
            tags = s.get("tags") or {}
            audio_tracks.append({"i": len(audio_tracks),
                                 "lang": (tags.get("language") or "").lower(),
                                 "title": tags.get("title") or "",
                                 "codec": s.get("codec_name") or ""})
        elif s.get("codec_type") == "subtitle":
            sub_n += 1
    pref = _preferred_audio(audio_tracks)
    return {"available": True, "audio": len(audio_codecs),
            "subtitle": sub_n, "audio_codecs": audio_codecs,
            "audio_tracks": audio_tracks,
            "preferred_audio": pref,
            "duration": duration,
            "needs_transcode": _needs_transcode(path, audio_tracks, pref)}


# 浏览器能直解的音频编码。Chrome/Edge 连 MKV 容器里的 ac3 也能解；
# 但 eac3(DDP/DD+ 5.1) 实测在 Mac Chrome 上 MKV/MP4 都无声，一律转 AAC
BROWSER_AUDIO_CODECS = {
    "aac", "ac3", "mp3", "opus", "vorbis", "flac", "alac", "mp2",
    "pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_u8",
}


def _needs_transcode(path: Path, audio_tracks: list, preferred: int) -> bool:
    """按实际会播放的优选音轨判断是否要服务端转 AAC。"""
    if not audio_tracks:
        return False
    codec = (audio_tracks[min(preferred, len(audio_tracks) - 1)]
             .get("codec") or "").lower()
    return codec not in BROWSER_AUDIO_CODECS


async def _probe_streams(path: Path):
    """ffprobe JSON 探测全部流（含 language/title 标签）；失败返回 None。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries",
            "stream=index,codec_type,codec_name:stream_tags=language,title:format=duration",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return json.loads(out.decode(errors="ignore") or "{}")
    except (FileNotFoundError, asyncio.TimeoutError, ValueError):
        return None


# 音轨语言识别：ffprobe 的 language 标签是 ISO 639 码，命名习惯各异，归一成 zh/en/ja
_LANG_ALIASES = (
    ("zh", {"zh", "chi", "zho", "cmn", "yue"}, ("中文", "国语", "普通话", "粤")),
    ("en", {"en", "eng"}, ("english",)),
    ("ja", {"ja", "jp", "jpn"}, ("日语", "日文", "japanese")),
)


def _lang_of(track: dict) -> str:
    lang = (track.get("lang") or "").lower()
    title = (track.get("title") or "").lower()
    for canon, codes, words in _LANG_ALIASES:
        if lang in codes or any(w in title for w in words):
            return canon
    return lang


def _preferred_audio(tracks: list) -> int:
    """多音轨时优先 中→英→日，都没有则第 0 条（改优先级只动这里）。"""
    for want in ("zh", "en", "ja"):
        for t in tracks:
            if _lang_of(t) == want:
                return t["i"]
    return 0


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
    if request.query_params.get("audio") == "aac":
        return await _stream_aac(path, request)
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


async def _seek_landing(path: Path, t: float) -> float:
    """实测输入寻址 `-ss t` 时视频流的真实落点（copyts 保留绝对时间戳）。
    MKV 的关键帧寻址很怪：-ss 恰好落在关键帧时刻附近会多退一个 GOP
    （实测 -ss 598.648 落 596.596），所以不能靠"关键帧表 + epsilon"推断，
    必须实测。只拷 1 帧不解码，很快；失败时原样返回 t。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-copyts",
            "-i", str(path), "-map", "0:v:0", "-c:v", "copy",
            "-frames:v", "1", "-f", "framemd5", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        tb = 1000.0
        for line in out.decode(errors="ignore").splitlines():
            if line.startswith("#tb 0:"):
                # 形如 "#tb 0: 1/1000"（注意是空格分隔）
                m = re.search(r"1/(\d+)", line)
                if m:
                    tb = float(m.group(1))
            elif line and not line.startswith("#"):
                parts = line.split(",")
                if len(parts) >= 3:
                    try:
                        return max(0.0, int(parts[2].strip()) / tb)
                    except ValueError:
                        pass
                break
    except (OSError, asyncio.TimeoutError):
        pass
    return t


@app.get("/api/stream-prep/{media_id}")
async def stream_prep(media_id: int, t: float = 0):
    """转码起流前的关键帧对齐：返回 `-ss t` 时视频流的实际落点（≤ t）。"""
    row = db.one("SELECT path FROM media WHERE id=?", (media_id,))
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    t = min(max(0.0, t or 0.0), 24 * 3600)
    start = await _seek_landing(path, t) if t > 0 else 0.0
    return {"start": round(start, 3)}


async def _stream_aac(path: Path, request: Request):
    """浏览器解不了的音轨（AC3/EAC3/DTS/TrueHD）→ ffmpeg 实时转 AAC，
    视频流原样拷贝（不重编码，CPU 占用低）。转码流无法按字节 seek，
    前端拖进度条改用 ?t=秒 重新起流；客户端断开即杀 ffmpeg。"""
    try:
        t = max(0.0, float(request.query_params.get("t", 0) or 0))
    except ValueError:
        t = 0.0
    data = await _probe_streams(path) or {}
    vcodec = next((s.get("codec_name") for s in data.get("streams", [])
                   if s.get("codec_type") == "video"), "")
    # 音轨选择：?a=N 指定第 N 条音轨；缺省按 中→英→日 优选（多音轨影片自动说中文/英文）
    try:
        a = int(request.query_params.get("a", "") or -1)
    except ValueError:
        a = -1
    if a < 0:
        tracks = [{"i": n,
                   "lang": (s.get("tags") or {}).get("language", ""),
                   "title": (s.get("tags") or {}).get("title", "")}
                  for n, s in enumerate(x for x in data.get("streams", [])
                                        if x.get("codec_type") == "audio")]
        a = _preferred_audio(tracks)
    # 视频流是整包拷贝，-ss 输入寻址只能落关键帧；音频转码则是精确落点。
    # muxer 按最早 pts 归零时间轴，两路 -ss 不同就会音画错位（不同步的根因）。
    # 做法：先实测视频真实落点 V；视频输入按 t 寻址（落 V，首帧 pts=V-t），
    # 音频走第二输入精确从 V 起、解码后用 asetpts 平移 (V-t) 与视频首帧对齐
    # （不能用 -itsoffset：它会让 accurate seek 的丢弃边界算错）。
    v_start = await _seek_landing(path, t) if t else 0.0
    cmd = ["ffmpeg", "-v", "error"]
    if t:
        cmd += ["-ss", f"{t:.3f}"]
    cmd += ["-i", str(path)]
    if t:
        cmd += ["-ss", f"{v_start:.3f}", "-i", str(path)]
    cmd += ["-map", "0:v:0", "-c:v", "copy"]
    if vcodec == "hevc":
        cmd += ["-tag:v", "hvc1"]  # 帮 Mac/浏览器识别走硬解
    cmd += ["-map", f"{1 if t else 0}:a:{a}"]
    if t and v_start < t - 0.001:
        cmd += ["-af", f"asetpts=PTS-{t - v_start:.3f}/TB"]
    cmd += ["-c:a", "aac", "-b:a", "192k",
            # 源文件章节信息会变成多余的 text/data 轨，去掉
            "-map_chapters", "-1"]
    # 不用 empty_moov：让 moov 带上整部影片时长，浏览器进度条才可拖
    # （empty_moov 时长为 0，Chrome 当直播流处理，进度条锁死）
    cmd += ["-movflags", "frag_keyframe+default_base_moof",
            "-f", "mp4", "pipe:1"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def gen():
        try:
            while True:
                chunk = await proc.stdout.read(262144)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    return StreamingResponse(gen(), media_type="video/mp4")


_VTT_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})\.(\d{3})$")
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")


def _strip_ass_tags(text: str) -> str:
    """清掉字幕正文里的 ASS 覆写标签（{\\fscx70\\1c&H...&} {\\r} 等），
    \\N/\\n 是 ASS 硬换行、\\h 是硬空格，转成 VTT 认识的形态。"""
    text = _ASS_TAG_RE.sub("", text)
    return (text.replace("\\N", "\n").replace("\\n", "\n")
            .replace("\\h", " "))


def _vtt_ts_to_sec(s: str):
    m = _VTT_TS_RE.match(s)
    if not m:
        return None
    return (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
            + int(m.group(3)) + int(m.group(4)) / 1000)


def _sec_to_vtt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    return f"{h:02d}:{m:02d}:{sec - h * 3600 - m * 60:06.3f}"


def _shift_vtt(text: str, offset: float) -> str:
    """转码流从 offset 秒重起时时间轴从 0 开始，而字幕是绝对时间：
    把所有 cue 前移 offset 秒；整体落在 offset 之前的 cue 丢弃。"""
    if offset <= 0:
        return text
    out, lines = [], text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = re.match(r"^(\S+) --> (\S+)(.*)$", line)
        if m:
            st, en = _vtt_ts_to_sec(m.group(1)), _vtt_ts_to_sec(m.group(2))
            if st is not None and en is not None:
                j = i + 1
                body = []
                while j < n and lines[j].strip():
                    body.append(lines[j])
                    j += 1
                if en - offset > 0:
                    out.append(f"{_sec_to_vtt_ts(st - offset)} --> "
                               f"{_sec_to_vtt_ts(en - offset)}{m.group(3)}")
                    out.extend(body)
                elif out and out[-1].strip() and " --> " not in out[-1] \
                        and out[-1].strip() != "WEBVTT":
                    out.pop()  # 整条 cue 丢弃时，上一行的 cue 编号也一起去掉
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


@app.get("/api/subtitle/{media_id}")
async def subtitle(media_id: int, offset: float = 0):
    return await subtitle_track(media_id, 0, offset)


@app.get("/api/subtitle/{media_id}/{idx}")
async def subtitle_track(media_id: int, idx: int, offset: float = 0):
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
        # 文件被移动过（subs JSON 里存的是旧绝对路径）：
        # 用视频所在目录 + 字幕文件名兜底，多数移动都能自愈
        vrow = db.one("SELECT path FROM media WHERE id=?", (media_id,))
        if vrow:
            alt = Path(vrow["path"]).parent / p.name
            if alt.exists():
                p = alt
        if not p.exists():
            raise HTTPException(404, "字幕文件不存在")
    offset = min(max(0.0, offset or 0.0), 24 * 3600)
    text = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() == ".srt":
        vtt = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})",
                                    r"\1.\2", text)
        return Response(_shift_vtt(_strip_ass_tags(vtt), offset),
                        media_type="text/vtt; charset=utf-8")
    if p.suffix.lower() == ".vtt":
        return Response(_shift_vtt(_strip_ass_tags(text), offset),
                        media_type="text/vtt; charset=utf-8")
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
        # subs JSON 数组与 sub_path 里存的是字幕绝对路径，同样按前缀平移
        db.exe("UPDATE media SET subs = REPLACE(subs, ?, ?) WHERE subs LIKE ?",
               (str(src) + "/", str(dst) + "/", "%" + str(src) + "/%"))
        db.exe("UPDATE media SET sub_path = ? || '/' || substr(sub_path, ?) WHERE sub_path LIKE ?",
               (str(dst), len(str(src)) + 2, str(src) + "/%"))
    else:
        db.exe("UPDATE media SET path=? WHERE path=?", (str(dst), str(src)))
        db.exe("UPDATE downloads SET path=? WHERE path=?", (str(dst), str(src)))
        db.exe("UPDATE media SET sub_path=? WHERE sub_path=?", (str(dst), str(src)))


def _check_not_root(p: Path):
    if p == MEDIA_ROOT.resolve():
        raise HTTPException(403, "不能对媒体根目录本身操作")


def _lib_type_of(p: Path) -> str | None:
    """路径落在电影/剧集库目录内时返回 'movie'/'tv'，否则 None。"""
    from . import scanner
    for root, lt in ((scanner.movie_dir(), "movie"), (scanner.tv_dir(), "tv")):
        root = root.resolve()
        if p == root or root in p.parents:
            return lt
    return None


async def _rescan_moved(dst: Path, is_dir: bool):
    """文件管理移动/改名后同步影库：搬进播放库的视频立即入库（字幕等忽略）；
    搬到库外的条目从影库删除——影库只收录指定电影/剧集目录内的文件。"""
    from . import parser, scanner
    lib = _lib_type_of(dst)
    if lib is None:
        if is_dir:
            db.exe("DELETE FROM media WHERE path LIKE ?", (str(dst) + "/%",))
        else:
            db.exe("DELETE FROM media WHERE path=?", (str(dst),))
        return
    try:
        if is_dir:
            for files in scanner._walk(dst):
                for f in files:
                    await scanner.scan_file(f, lib)
        elif parser.is_video(dst.name):
            await scanner.scan_file(dst, lib)
        else:
            # 库内改名成非视频（如字幕）：不上墙
            db.exe("DELETE FROM media WHERE path=?", (str(dst),))
    except Exception as e:
        print(f"[fs] 移动后同步影库失败: {e}")


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
    asyncio.create_task(_rescan_moved(dst, is_dir))
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
    asyncio.create_task(_rescan_moved(dst, is_dir))
    return {"ok": True, "path": str(dst)}
