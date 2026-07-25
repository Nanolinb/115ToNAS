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
    subs = [s for s in subs if s.get("filelist")]
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
            r = await client.get(f"{ASSRT}/sub/search",
                                 params={"token": token, "q": query, "cnt": 10})
            subs = ((r.json().get("sub") or {}).get("subs")) or []
            if not subs and year:  # 去掉年份再搜
                r = await client.get(f"{ASSRT}/sub/search",
                                     params={"token": token, "q": title, "cnt": 10})
                subs = ((r.json().get("sub") or {}).get("subs")) or []
            chosen = _pick_sub(subs)
            if not chosen:
                return None
            r = await client.get(f"{ASSRT}/sub/detail",
                                 params={"token": token, "id": chosen["id"]})
            detail_subs = ((r.json().get("sub") or {}).get("subs")) or []
            files = detail_subs[0].get("filelist") if detail_subs else chosen.get("filelist")
            if not files:
                return None
            url = files[0].get("url")
            if not url:
                return None
            r = await client.get(url, follow_redirects=True)
            if r.status_code != 200 or len(r.content) < 20:
                return None
            raw = r.content
            # 判定格式：ass 字幕有 [Script Info]，其余按 srt 处理
            head = raw[:200].decode("utf-8", errors="ignore")
            ext = ".ass" if "[Script Info]" in head else ".srt"
            dest = video_path.with_suffix("").with_name(
                video_path.stem + ".zh" + ext)
            dest.write_bytes(_to_utf8(raw))
            return dest
    except Exception:
        return None


def _to_utf8(raw: bytes) -> bytes:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc).encode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return raw
