"""字幕模块：
1. 本地匹配：视频旁已有同名字幕直接挂上；
2. 在线搜刮：射手网(伪) assrt.net 开放 API（需要在设置里填免费 token）。
"""
from pathlib import Path

import httpx

from . import db
from .parser import SUB_EXTS

ASSRT = "https://api.assrt.net/v1"


def find_local_sub(video_path: Path) -> Path | None:
    """查找视频旁边的同名字幕（含 .zh/.chs/.cht/.sc 等中缀）。"""
    stem = video_path.stem.lower()
    try:
        for f in video_path.parent.iterdir():
            if f.suffix.lower() in SUB_EXTS and f.stem.lower().startswith(stem):
                return f
    except OSError:
        pass
    return None


def _pick_sub(subs: list) -> dict | None:
    def score(s):
        lang = (s.get("lang") or {}).get("langlist") or {}
        sc = 0
        if lang.get("langchs"):
            sc += 3
        if lang.get("langdou"):
            sc += 2
        if lang.get("langcht"):
            sc += 1
        return sc
    # 注意：搜索结果不带 filelist（要调 detail 接口才有），只要求有 id
    subs = [s for s in subs if s.get("id")]
    if not subs:
        return None
    return max(subs, key=score)


async def search_and_download(video_path: Path, title: str, year: int | None) -> Path | None:
    token = db.get_secret("assrt_token", "").strip()
    if not token or not title:
        return None
    query = f"{title} {year}" if year else title
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            subs = await _search(client, token, query)
            if not subs and year:  # 去掉年份再搜
                subs = await _search(client, token, title)
            if not subs and title:
                # 英文名找不到时，去掉标点再试一次（如 The.First.Slam.Dunk）
                alt = title.replace(".", " ").replace("_", " ").strip()
                if alt != title:
                    subs = await _search(client, token, alt)
            chosen = _pick_sub(subs)
            if not chosen:
                print(f"[subtitles] assrt 无匹配结果: {query}")
                return None
            detail_subs = await _detail(client, token, chosen["id"])
            s0 = detail_subs[0] if detail_subs else chosen
            files = s0.get("filelist") or []
            # 多文件字幕包走 filelist，单文件字幕直接在条目上带 url
            url = (files[0].get("url") if files else None) or s0.get("url")
            if not url:
                print(f"[subtitles] assrt detail 无下载地址: id={chosen['id']}")
                return None
            raw = await _fetch(client, url)
            if raw is None or len(raw) < 20:
                print(f"[subtitles] 字幕文件下载失败: {url[:80]}")
                return None
            # 判定格式：ass 字幕有 [Script Info]，其余按 srt 处理
            head = raw[:200].decode("utf-8", errors="ignore")
            ext = ".ass" if "[Script Info]" in head else ".srt"
            dest = video_path.with_suffix("").with_name(
                video_path.stem + ".zh" + ext)
            dest.write_bytes(_to_utf8(raw))
            print(f"[subtitles] 字幕已保存: {dest.name}")
            return dest
    except Exception as e:
        print(f"[subtitles] 搜刮失败 {title}: {type(e).__name__} {e}")
        return None


async def _get_json(client: httpx.AsyncClient, url: str, params: dict,
                    retries: int = 3) -> dict:
    """assrt API 请求：网络瞬断有限重试。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = await client.get(url, params=params)
            return r.json()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                import asyncio
                await asyncio.sleep(1.2 * (attempt + 1))
    raise last


async def _search(client: httpx.AsyncClient, token: str, q: str) -> list:
    j = await _get_json(client, f"{ASSRT}/sub/search",
                        {"token": token, "q": q, "cnt": 10})
    return ((j.get("sub") or {}).get("subs")) or []


async def _detail(client: httpx.AsyncClient, token: str, sub_id) -> list:
    j = await _get_json(client, f"{ASSRT}/sub/detail",
                        {"token": token, "id": sub_id})
    return ((j.get("sub") or {}).get("subs")) or []


async def _fetch(client: httpx.AsyncClient, url: str, retries: int = 3) -> bytes | None:
    for attempt in range(retries):
        try:
            r = await client.get(url, follow_redirects=True)
            if r.status_code == 200:
                return r.content
        except Exception:
            if attempt < retries - 1:
                import asyncio
                await asyncio.sleep(1.2 * (attempt + 1))
    return None


def _to_utf8(raw: bytes) -> bytes:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc).encode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return raw
