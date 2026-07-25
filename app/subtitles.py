"""字幕模块：
1. 本地匹配：视频旁已有同名字幕直接挂上（严格前缀 + 模糊匹配）；
2. 在线搜刮：射手网(伪) assrt.net 开放 API（需要在设置里填免费 token）。
"""
import re
from pathlib import Path

import httpx

from . import db
from .parser import SUB_EXTS, _TAG_RE

ASSRT = "https://api.assrt.net/v1"

# 字幕轨道语言元数据
LANG_META = {
    "zh": "中文字幕", "en": "English", "cht": "繁体中文", "zh-en": "中英双语",
}
# 在线搜刮的目标语言与文件后缀
TRACK_TARGETS = (("zh", ".zh"), ("en", ".en"), ("zh-en", ".zh-en"))


def _detect_lang(infix: str) -> str:
    """从字幕文件名中缀识别语言（如 .zh / .en / .cht / .zh-en / .chs / .双语）。"""
    n = infix.lower()
    for key, pats in (("zh-en", (".zh-en", ".zh_en", ".chs-eng", ".zho-eng", ".双语", ".简英")),
                      ("cht", (".cht", ".tc", ".zht", ".繁")),
                      ("zh", (".zh", ".chs", ".sc", ".zhs", ".cn", ".简")),
                      ("en", (".en", ".eng", ".英"))):
        if any(p in n for p in pats):
            return key
    return "zh"


# 集数标记：S01E02 / E02 / EP02 / 第2集 —— 模糊匹配时剧集必须集数一致，防错挂
_EP_KEY_RE = re.compile(
    r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})|[Ee][Pp]?(\d{1,3})|第\s*(\d{1,3})\s*[集话回]")


def _ep_key(stem: str) -> str | None:
    m = _EP_KEY_RE.search(stem)
    if not m:
        return None
    if m.group(1) is not None:
        return f"s{int(m.group(1)):02d}e{int(m.group(2)):03d}"
    return f"e{int(m.group(3) or m.group(4)):03d}"


def _norm(stem: str) -> str:
    """归一化文件名主干：去集数标记与发布标签（1080p/BluRay/x265…），只留字母数字与中文。"""
    t = _EP_KEY_RE.sub(" ", stem)
    t = _TAG_RE.sub(" ", t)
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", t).lower()


def _fuzzy_match(video_stem: str, sub_stem: str) -> bool:
    """模糊匹配：任一方有集数标记 → 两边集数必须一致；
    否则（电影）归一化后互为前缀即匹配（Movie.2020.1080p ≈ Movie.2020.zh）。"""
    vek, sek = _ep_key(video_stem), _ep_key(sub_stem)
    if vek or sek:
        return bool(vek and sek and vek == sek)
    a, b = _norm(video_stem), _norm(sub_stem)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def find_local_sub(video_path: Path) -> Path | None:
    """查找视频旁边的同名字幕（含 .zh/.chs/.cht/.sc 等中缀）。"""
    tracks = find_all_local_subs(video_path)
    return Path(tracks[0]["path"]) if tracks else None


def find_all_local_subs(video_path: Path) -> list:
    """扫描视频旁全部同名字幕，识别为轨道列表 [{lang,label,path}]。每种语言取第一个。
    匹配优先级：严格前缀（同名）> 模糊匹配 > 可播格式（vtt/srt 优先于 ass）。"""
    stem = video_path.stem.lower()
    out = []
    try:
        for f in sorted(video_path.parent.iterdir()):
            if f.suffix.lower() not in SUB_EXTS:
                continue
            fstem = f.stem.lower()
            exact = fstem.startswith(stem)
            if not exact and not _fuzzy_match(video_path.stem, f.stem):
                continue
            lang = _detect_lang(fstem[len(stem):] if exact else fstem)
            out.append({"lang": lang, "label": LANG_META[lang], "path": str(f),
                        "_rank": 0 if exact else 1,
                        "_dlen": len(fstem) - len(stem) if exact else len(fstem)})
    except OSError:
        pass
    # 同一语言可能有多条候选：严格同名 > 模糊匹配 > 与视频名最接近（中缀短）> 可播格式
    fmt_rank = {".vtt": 0, ".srt": 1}
    seen, uniq = set(), []
    for t in sorted(out, key=lambda t: (t["lang"], t["_rank"], t["_dlen"],
                                        fmt_rank.get(Path(t["path"]).suffix.lower(), 2))):
        if t["lang"] not in seen:
            seen.add(t["lang"])
            t.pop("_rank")
            t.pop("_dlen")
            uniq.append(t)
    return uniq


def _langflags(s: dict) -> dict:
    return (s.get("lang") or {}).get("langlist") or {}


def _pick_for(subs: list, target: str) -> dict | None:
    """按目标语言挑选候选：zh=纯简中，en=纯英文，zh-en=双语合并。"""
    def ok(s):
        L = _langflags(s)
        chs = bool(L.get("langchs"))
        cht = bool(L.get("langcht"))
        eng = bool(L.get("langeng"))
        dou = bool(L.get("langdou"))
        if target == "zh":
            return chs and not eng and not dou
        if target == "en":
            return eng and not chs and not cht and not dou
        if target == "zh-en":
            return dou or (chs and eng)
        return False
    cands = [s for s in subs if s.get("id") and ok(s)]
    if not cands and target == "zh":
        # 宽松回退：任何含简中的（含双语条目的简中部分）
        cands = [s for s in subs if s.get("id") and _langflags(s).get("langchs")]
    return cands[0] if cands else None


async def search_and_download(video_path: Path, title: str, year: int | None) -> list:
    """在线搜刮多语言字幕（简中 / 英文 / 中英双语），返回新保存的轨道列表。"""
    token = db.get_secret("assrt_token", "").strip()
    if not token or not title:
        return []
    saved = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            subs = await _search_candidates(client, token, title, year)
            if not subs:
                print(f"[subtitles] assrt 无匹配结果: {title} {year or ''}")
                return []
            for target, suffix in TRACK_TARGETS:
                chosen = _pick_for(subs, target)
                if not chosen:
                    continue
                track = await _download_one(client, token, chosen,
                                            video_path, suffix, target)
                if track:
                    saved.append(track)
    except Exception as e:
        print(f"[subtitles] 搜刮失败 {title}: {type(e).__name__} {e}")
    return saved


async def _search_candidates(client: httpx.AsyncClient, token: str,
                             title: str, year: int | None) -> list:
    query = f"{title} {year}" if year else title
    subs = await _search(client, token, query)
    if not subs and year:  # 去掉年份再搜
        subs = await _search(client, token, title)
    if not subs and title:
        # 英文名找不到时，去掉标点再试一次（如 The.First.Slam.Dunk）
        alt = title.replace(".", " ").replace("_", " ").strip()
        if alt != title:
            subs = await _search(client, token, alt)
    return subs


async def _download_one(client: httpx.AsyncClient, token: str, chosen: dict,
                        video_path: Path, suffix: str, lang: str) -> dict | None:
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
    dest = video_path.with_suffix("").with_name(video_path.stem + suffix + ext)
    if dest.exists():
        return {"lang": lang, "label": LANG_META[lang], "path": str(dest)}
    dest.write_bytes(_to_utf8(raw))
    print(f"[subtitles] 字幕已保存: {dest.name}")
    return {"lang": lang, "label": LANG_META[lang], "path": str(dest)}


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
