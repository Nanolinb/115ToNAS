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
# 轨道展示/默认轨排序：中文优先
LANG_ORDER = {"zh": 0, "zh-en": 1, "cht": 2, "en": 3}


def _detect_lang(infix: str) -> str:
    """从字幕文件名中缀识别语言（如 .zh / .en / .cht / .zh-en / .chs / .双语）。
    先剥发布标签且只认结尾后缀：发行名里的音轨标记（ITA.ENG 等）不再误判为字幕语言。"""
    n = _TAG_RE.sub(" ", infix.lower()).strip()
    n = re.sub(r"[ .\[\]_-]+$", "", n)
    for key, pats in (("zh-en", (".zh-en", ".zh_en", ".chs-eng", ".zho-eng", ".双语", ".简英")),
                      ("cht", (".cht", ".tc", ".zht", ".繁")),
                      ("zh", (".zh", ".chs", ".sc", ".zhs", ".cn", ".简")),
                      ("en", (".en", ".eng", ".英"))):
        if any(n.endswith(p) for p in pats):
            return key
    # 中文标记允许出现在任意位置（如「.简.修复版」），英文必须结尾
    for key, pats in (("zh-en", (".双语", ".简英")), ("cht", (".繁",)), ("zh", (".简",))):
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


_SEASON_RE = re.compile(r"[Ss](\d{1,2})(?![ ._-]*[Ee]\d)")


def _season_key(stem: str) -> int | None:
    """季号：S02E01 或裸 S02 / Season.2 都能取到。"""
    m = re.search(r"[Ss](\d{1,2})[ ._-]*[Ee]\d", stem) or _SEASON_RE.search(stem)
    if not m:
        m = re.search(r"[Ss]eason[ ._-]?(\d{1,2})", stem, re.I)
    return int(m.group(1)) if m else None


def relevance(video_stem: str, desc: str) -> int:
    """字幕候选与视频文件名的相关度打分（-1 = 硬冲突，淘汰）。

    剧集视频：候选集数不一致 → -1；集数精确匹配 100+词元重合（强信号，
    即使描述没带剧名也认）；只有季号且同季 → 30+重合，但零重合 = 别剧同季
    （搜 1923 出 Superman.And.Lois.S01 的教训），直接 -1；
    无任何集数/季号信息只给词元重合分（调用方用门槛挡掉动漫同名单集）。
    电影视频：候选带集数标记 → -1（电影不该挂剧集字幕）。"""
    vek, dek = _ep_key(video_stem), _ep_key(desc)
    vt = set(_TOKEN_RE.findall(_TAG_RE.sub(" ", video_stem).lower()))
    dt = set(_TOKEN_RE.findall(_TAG_RE.sub(" ", desc).lower()))
    ov = len(vt & dt)
    if vek:
        if dek:
            return 100 + ov if vek == dek else -1
        # 视频有集数、候选没有：看季号
        vs, ds = _season_key(video_stem), _season_key(desc)
        if ds is not None:
            if vs is not None and ds != vs:
                return -1
            return 30 + ov if ov > 0 else -1
        return ov
    if dek:  # 电影挂剧集字幕
        return -1
    return ov


def min_relevance(video_stem: str) -> int:
    """候选入选门槛：剧集必须有集/季对应关系，电影至少沾一个词元。"""
    return 30 if _ep_key(video_stem) else 1


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
    # 同一语言可能有多条候选：中文优先 > 严格同名 > 模糊匹配 > 与视频名最接近 > 可播格式
    fmt_rank = {".vtt": 0, ".srt": 1}
    seen, uniq = set(), []
    for t in sorted(out, key=lambda t: (LANG_ORDER.get(t["lang"], 9), t["_rank"],
                                        t["_dlen"],
                                        fmt_rank.get(Path(t["path"]).suffix.lower(), 2))):
        if t["lang"] not in seen:
            seen.add(t["lang"])
            t.pop("_rank")
            t.pop("_dlen")
            uniq.append(t)
    return uniq


def _langflags(s: dict) -> dict:
    return (s.get("lang") or {}).get("langlist") or {}


def _rank_for(subs: list, target: str, video_stem: str = "") -> list:
    """按目标语言筛候选并依相关度排序（高→低）：zh=纯简中，en=纯英文，zh-en=双语合并。
    候选须过文件名相关度门槛（防「搜 One Piece 挂到动漫版」）。
    返回列表供调用方逐个尝试——第一条是 7z/失效链接时能落到下一条。"""
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

    def score(s):
        desc = f"{s.get('native_name') or ''} {s.get('videoname') or ''}"
        return relevance(video_stem, desc)

    floor = min_relevance(video_stem)
    cands = [s for s in subs if s.get("id") and ok(s) and score(s) >= floor]
    if not cands and target == "zh":
        # 宽松回退：任何含简中的（含双语条目的简中部分），同样要过相关度门槛
        cands = [s for s in subs if s.get("id") and _langflags(s).get("langchs")
                 and score(s) >= floor]
    cands.sort(key=score, reverse=True)
    return cands


async def search_and_download(video_path: Path, title: str, year: int | None) -> list:
    """在线搜刮多语言字幕（简中 / 英文 / 中英双语），返回新保存的轨道列表。
    来源顺序：assrt（需 token）→ subhd.cc（免 token），只补缺的语言。
    剧集带 SxxExx 集数查询，且优先用文件名前缀当搜索标题——数据库标题可能是
    刮削错的本地化条目（1923 S01E02+ 被匹配成意大利语名），文件名才是最可靠的。"""
    if not title:
        return []
    ep = _ep_tag(video_path.stem)
    queries = []
    if ep:
        file_title = _title_from_stem(video_path.stem)
        if file_title and file_title.lower() != title.lower():
            queries += [f"{file_title} {ep}", f"{title} {ep}"]
        else:
            queries.append(f"{title} {ep}")
    queries.append(f"{title} {year}" if year else title)
    if year:
        queries.append(title)
    alt = title.replace(".", " ").replace("_", " ").strip()
    if alt != title:
        queries.append(alt)
    # 保序去重
    seen = set()
    queries = [q for q in queries if not (q in seen or seen.add(q))]
    saved = []
    if db.get_secret("assrt_token", "").strip():
        saved += await _assrt_download(video_path, queries)
    have = {t["lang"] for t in saved}
    missing = [(t, sfx) for t, sfx in TRACK_TARGETS if t not in have]
    if missing:
        saved += await _subhd_download(video_path, queries, missing)
    return saved


_EP_TAG_RE = re.compile(r"[sS]\d{1,2}[eE]\d{1,3}")


def _ep_tag(stem: str) -> str | None:
    """从视频文件名提取 S01E01 集数标记（大写零填充），电影返回 None。"""
    m = _EP_TAG_RE.search(stem)
    return m.group(0).upper() if m else None


def _title_from_stem(stem: str) -> str | None:
    """文件名里集数标记之前的部分当标题（1923.S01E02.xxx → 1923）。"""
    m = _EP_TAG_RE.search(stem)
    if not m:
        return None
    t = stem[:m.start()].rstrip("._- ").replace(".", " ").replace("_", " ").strip()
    return t or None


async def _assrt_download(video_path: Path, queries: list) -> list:
    """assrt.net 搜刮（需要设置页配置 token）。"""
    token = db.get_secret("assrt_token", "").strip()
    saved = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            subs = []
            for q in queries:
                subs = await _search(client, token, q)
                if subs:
                    break
            if not subs:
                print(f"[subtitles] assrt 无匹配结果: {queries}")
                return []
            stem = video_path.stem
            used_ids = set()
            for target, suffix in TRACK_TARGETS:
                # 按相关度逐个尝试：7z 拒收/链接失效时下一位候选补上
                for chosen in _rank_for(subs, target, stem):
                    # 同一条目不重复下载（zh 宽松回退与 zh-en 可能选中同一条双语，
                    # 否则同一文件会以两个语言后缀各存一份）
                    if chosen["id"] in used_ids:
                        continue
                    used_ids.add(chosen["id"])
                    track = await _download_one(client, token, chosen,
                                                video_path, suffix, target)
                    if track:
                        saved.append(track)
                        break
    except Exception as e:
        print(f"[subtitles] 搜刮失败 {queries}: {type(e).__name__} {e}")
    return saved


_TOKEN_RE = re.compile(r"[a-z0-9]+")


async def _subhd_download(video_path: Path, queries: list, targets: list) -> list:
    """subhd.cc 搜刮（免 token）：按与文件名的相关度排序后逐语言挑第一条。"""
    from . import subhd
    saved = []
    try:
        async with subhd.client() as cli:
            items = []
            for kw in queries:
                items = await subhd.search_subs(cli, kw)
                if items:
                    break
            if not items:
                print(f"[subtitles] subhd 无匹配结果: {queries}")
                return []
            vstem = video_path.stem
            floor = min_relevance(vstem)
            scored = [it for it in items if relevance(vstem, it["desc"]) >= floor]
            scored.sort(key=lambda it: relevance(vstem, it["desc"]), reverse=True)
            for target, suffix in targets:
                # 按相关度逐个尝试：7z 拒收/下载失败时下一位候选补上
                for cand in [it for it in scored if it["lang"] == target]:
                    raw = await subhd.download_sub(cli, cand["sid"])
                    if not raw:
                        continue
                    track = _save_raw(video_path, raw, suffix, target)
                    if track:
                        saved.append(track)
                        break
    except Exception as e:
        print(f"[subtitles] subhd 搜刮失败 {queries}: {type(e).__name__} {e}")
    return saved


def _pick_ep_name(names: list, video_stem: str) -> str | None:
    """字幕包/多文件条目中挑与视频集数对应的那个文件。
    支持裸数字文件名（季包内 01.ass/02.ass 按集数编号对齐）；
    多集包认不出集数时返回 None（宁缺毋错，不再错挂包内第一个）。"""
    vek = _ep_key(video_stem)
    if vek:
        for n in names:
            if _ep_key(Path(n).stem) == vek:
                return n
        want = int(re.search(r"e(\d{3})$", vek).group(1))
        for n in names:
            stem = Path(n).stem.strip()
            if re.fullmatch(r"\d{1,3}", stem) and int(stem) == want:
                return n
        if len(names) > 1:
            return None
    return names[0] if names else None


def _save_raw(video_path: Path, raw: bytes, suffix: str, lang: str) -> dict | None:
    """把字幕内容存到视频旁（<视频主干><语言后缀>.<扩展名>）。
    zip 包按集数挑文件；7z/rar/gzip 无法解就拒收（防二进制垃圾存成 .srt）；
    格式识别在转码后进行（UTF-16 的 ASS 也能认出来）。"""
    if raw[:4] == b"PK\x03\x04":  # zip 字幕包
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = [n for n in z.namelist()
                         if Path(n).suffix.lower() in SUB_EXTS
                         and not Path(n).name.startswith(".")]
                if not names:
                    return None
                names.sort(key=lambda n: Path(n).suffix.lower() not in (".vtt", ".srt"))
                pick = _pick_ep_name(names, video_path.stem)
                if pick is None:
                    print(f"[subtitles] 季包内认不出对应集数，已拒收: {video_path.name}")
                    return None
                raw = z.read(pick)
        except zipfile.BadZipFile:
            return None
    elif raw[:6] == b"7z\xbc\xaf\x27\x1c":  # 7z 字幕包（字幕站常见）
        try:
            import io
            import tempfile
            import py7zr
            with py7zr.SevenZipFile(io.BytesIO(raw)) as z:
                names = [n for n in z.namelist()
                         if Path(n).suffix.lower() in SUB_EXTS
                         and not Path(n).name.startswith(".")]
                if not names:
                    return None
                names.sort(key=lambda n: Path(n).suffix.lower() not in (".vtt", ".srt"))
                pick = _pick_ep_name(names, video_path.stem)
                if pick is None:
                    print(f"[subtitles] 季包内认不出对应集数，已拒收: {video_path.name}")
                    return None
                with tempfile.TemporaryDirectory() as td:
                    z.extract(path=td, targets=[pick])
                    raw = (Path(td) / pick).read_bytes()
        except ImportError:
            print("[subtitles] 缺少 py7zr，无法解 7z 字幕包，已拒收")
            return None
        except Exception as e:
            print(f"[subtitles] 7z 解包失败: {type(e).__name__} {e}")
            return None
    elif raw[:6] in (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01") or raw[:2] == b"\x1f\x8b":
        print(f"[subtitles] 无法解压的字幕包格式，已拒收: {video_path.name}")
        return None
    raw = _to_utf8(raw)
    head = raw[:200].decode("utf-8", errors="ignore")
    if "[Script Info]" in head:
        ext = ".ass"
    elif head.startswith("WEBVTT"):
        ext = ".vtt"
    elif "-->" in raw[:4000].decode("utf-8", errors="ignore"):
        ext = ".srt"
    else:
        print(f"[subtitles] 下载内容不是字幕（HTML/二进制？），已拒收: {video_path.name}")
        return None
    dest = video_path.with_suffix("").with_name(video_path.stem + suffix + ext)
    if dest.exists():
        return {"lang": lang, "label": LANG_META[lang], "path": str(dest)}
    dest.write_bytes(raw)
    print(f"[subtitles] 字幕已保存: {dest.name}")
    return {"lang": lang, "label": LANG_META[lang], "path": str(dest)}


async def _download_one(client: httpx.AsyncClient, token: str, chosen: dict,
                        video_path: Path, suffix: str, lang: str) -> dict | None:
    detail_subs = await _detail(client, token, chosen["id"])
    s0 = detail_subs[0] if detail_subs else chosen
    files = s0.get("filelist") or []
    # 多文件字幕包（常见于整季打包）：挑与视频集数对应的文件，单文件直接取 url
    if files:
        pick = _pick_ep_name([f.get("filename") or f.get("f") or "" for f in files],
                             video_path.stem)
        if pick is None:
            print(f"[subtitles] 季包内认不出对应集数，跳过: id={chosen['id']}")
            return None
        f0 = next((f for f in files
                   if (f.get("filename") or f.get("f")) == pick), files[0])
        url = f0.get("url")
    else:
        url = s0.get("url")
    if not url:
        print(f"[subtitles] assrt detail 无下载地址: id={chosen['id']}")
        return None
    raw = await _fetch(client, url)
    if raw is None or len(raw) < 20:
        print(f"[subtitles] 字幕文件下载失败: {url[:80]}")
        return None
    return _save_raw(video_path, raw, suffix, lang)


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
                        {"token": token, "q": q, "cnt": 20})
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


def read_sub_text(p: Path) -> str:
    """读字幕文件为 UTF-8 文本：按 BOM/编码探测转换（UTF-16/GBK/Big5 都兼容）。
    服务端给播放器/浏览器喂字幕统一走这里，杜绝 UTF-16 原样输出成乱码。"""
    return _to_utf8(p.read_bytes()).decode("utf-8", errors="ignore")


def _to_utf8(raw: bytes) -> bytes:
    # 带 BOM 的先按 BOM 走（UTF-16 的字节流可能被 gb18030「成功」误解成乱码）
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig").encode("utf-8")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16").encode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc).encode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return raw
