"""媒体库扫描：遍历电影/剧集目录 → 解析文件名 → TMDB 匹配 → 本地/在线字幕。"""
import asyncio
import json
import time
from pathlib import Path

from . import db, parser, subtitles, tmdb_client
from .config import MEDIA_ROOT

scan_state = {"running": False, "last": 0, "message": ""}
_sem = asyncio.Semaphore(3)  # 限制元数据并发，保护 NAS CPU/内存


def movie_dir() -> Path:
    return Path(db.get_setting("movie_dir", str(MEDIA_ROOT / "movies")))


def tv_dir() -> Path:
    return Path(db.get_setting("tv_dir", str(MEDIA_ROOT / "tv")))


async def scan_all():
    if scan_state["running"]:
        return
    scan_state.update(running=True, message="扫描中…")
    try:
        await scan_directory(movie_dir(), "movie")
        await scan_directory(tv_dir(), "tv")
        scan_state["message"] = "扫描完成"
    except Exception as e:
        scan_state["message"] = f"扫描出错: {e}"
    finally:
        scan_state["running"] = False
        scan_state["last"] = int(time.time())


async def scan_directory(root: Path, lib_type: str):
    if not root.exists():
        return
    seen = set()
    for dirpath in _walk(root):
        for f in dirpath:
            seen.add(str(f))
            await scan_file(f, lib_type)
    # 标记已不存在的文件
    for row in db.q("SELECT id, path FROM media WHERE status='ok'"):
        if row["path"].startswith(str(root)) and row["path"] not in seen \
                and not Path(row["path"]).exists():
            db.exe("UPDATE media SET status='missing' WHERE id=?", (row["id"],))


def _walk(root: Path):
    """生成器式遍历，避免一次性加载全部路径到内存。"""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            files = []
            with __import__("os").scandir(current) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file() and parser.is_video(entry.name):
                        files.append(Path(entry.path))
            yield files
        except OSError:
            continue


async def scan_file(path: Path, lib_type: str, force: bool = False):
    """扫描单个文件：新文件入库 + 匹配元数据；已存在的按需更新。"""
    try:
        st = path.stat()
    except OSError:
        return
    row = db.one("SELECT * FROM media WHERE path=?", (str(path),))
    if row and not force and row["mtime"] == int(st.st_mtime) and row["size"] == st.st_size:
        return

    info = parser.parse(path.name)
    is_episode = info["episode"] is not None or lib_type == "tv"
    media_type = "episode" if is_episode else "movie"

    meta = None
    async with _sem:
        if tmdb_client.api_key():
            meta = await tmdb_client.match(
                info["title"], info["year"], "tv" if is_episode else "movie")

    tracks = subtitles.find_all_local_subs(path)
    values = {
        "type": media_type,
        "path": str(path),
        "title": (meta or {}).get("title") or info["title"],
        "year": (meta or {}).get("year") or info["year"],
        "season": info["season"],
        "episode": info["episode"],
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "tmdb_id": (meta or {}).get("tmdb_id"),
        "name_cn": (meta or {}).get("name_cn"),
        "poster": (meta or {}).get("poster") or (row or {}).get("poster"),
        "backdrop": (meta or {}).get("backdrop"),
        "overview": (meta or {}).get("overview"),
        "genres": (meta or {}).get("genres"),
        "rating": (meta or {}).get("rating"),
        "has_sub": 1 if tracks else 0,
        "sub_path": tracks[0]["path"] if tracks else None,
        "subs": json.dumps(tracks, ensure_ascii=False),
        "status": "ok",
        "created_at": int(time.time()),
    }
    if row:
        sets = ",".join(f"{k}=?" for k in values if k != "created_at")
        db.exe(f"UPDATE media SET {sets} WHERE id=?",
               [values[k] for k in values if k != "created_at"] + [row["id"]])
        media_id = row["id"]
    else:
        cols = ",".join(values)
        media_id = db.exe(f"INSERT INTO media({cols}) VALUES({','.join('?' * len(values))})",
                          list(values.values()))

    # 缺字幕语言（简中/英文/双语）且配置了 token → 在线补刮
    if db.get_secret("assrt_token", "").strip():
        have = {t["lang"] for t in tracks}
        if not {"zh", "en", "zh-en"} <= have:
            db.exe("UPDATE media SET sub_status='searching' WHERE id=?", (media_id,))
            title_q = (meta or {}).get("original_title") or info["title"]
            new_tracks = await subtitles.search_and_download(
                path, title_q, values["year"])
            if not new_tracks and title_q != info["title"]:
                new_tracks = await subtitles.search_and_download(
                    path, info["title"], values["year"])
            for t in new_tracks:
                if t["lang"] not in have:
                    tracks.append(t)
                    have.add(t["lang"])
            db.exe("""UPDATE media SET sub_status=?, has_sub=?, sub_path=?, subs=?
                      WHERE id=?""",
                   ("ok" if tracks else "failed", 1 if tracks else 0,
                    tracks[0]["path"] if tracks else None,
                    json.dumps(tracks, ensure_ascii=False), media_id))


async def rematch(media_id: int, tmdb_id: int | None = None,
                  title: str | None = None, year: int | None = None):
    row = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not row:
        return None
    kind = "tv" if row["type"] == "episode" else "movie"
    if tmdb_id:
        meta = await tmdb_client.match_by_id(tmdb_id, kind)
    else:
        meta = await tmdb_client.match(title or row["title"], year or row["year"], kind)
    if not meta:
        return None
    db.exe("""UPDATE media SET tmdb_id=?, title=?, name_cn=?, year=?, poster=?,
              backdrop=?, overview=?, genres=?, rating=? WHERE id=?""",
           (meta["tmdb_id"], meta["title"], meta["name_cn"], meta["year"],
            meta["poster"] or row["poster"], meta["backdrop"], meta["overview"],
            meta["genres"], meta["rating"], media_id))
    return meta
