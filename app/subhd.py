"""subhd.cc：公开字幕站（免 token，国内可直连）。

两个用途：
1. 字幕搜刮：assrt 之外的第二来源。下载链为
   搜索 /search/{kw} → POST /api/sub/prepare-download {sid} → GET /down/{sid}（中间页）
   → POST /api/sub/down {sid} → 文件直链（srt/ass 或 zip）。
2. 封面候选：搜索结果页直接附带豆瓣海报图（img.subhd.me，无防盗链），
   比百度图片更对口。
"""
import re
import html as _html

import httpx

from .config import UA

BASE = "https://subhd.cc"

_ITEM_RE = re.compile(r"href='/a/([0-9A-Za-z]+)'[^>]*>(.*?)</a>", re.S)
_POSTER_RE = re.compile(
    r"<a href='/d/\d+'>\s*<div class=\"pics\">\s*<img[^>]+src=\"(https://img\.subhd\.me/[^\"]+)\"")
_TAG_STRIP_RE = re.compile(r"<[^>]+>|\s+")


def client() -> httpx.AsyncClient:
    """带浏览器 UA 的会话（保留 cookie，下载链需要）。调用方负责关闭。"""
    return httpx.AsyncClient(timeout=20, follow_redirects=True,
                             headers={"User-Agent": UA, "Referer": BASE + "/"})


def _strip(s: str) -> str:
    return _TAG_STRIP_RE.sub(" ", s).strip()


def guess_lang(t: str) -> str:
    """从字幕描述与语言标签猜测轨道语言（与 subtitles 的 lang 体系一致）。"""
    if any(k in t for k in ("双语", "中英", "简英", "简繁", "繁英")):
        return "zh-en"
    zh = any(k in t for k in ("简体", "简中", "简】", "中文"))
    cht = any(k in t for k in ("繁体", "繁中", "繁】"))
    en = any(k in t for k in ("英语", "英文", "English", "英】"))
    if zh and en:
        return "zh-en"
    if zh:
        return "zh"
    if cht:
        return "cht"
    if en:
        return "en"
    return "zh"  # 中文站，默认简中


async def search_subs(cli: httpx.AsyncClient, keyword: str, limit: int = 15) -> list:
    """搜索字幕，返回 [{sid, desc, lang}]，desc 为适配版本描述（含发布组信息）。"""
    r = await cli.get(f"{BASE}/search/{keyword}")
    html = r.text
    items, order = {}, []
    for m in _ITEM_RE.finditer(html):
        sid, text = m.group(1), _strip(m.group(2))
        if not text:
            continue
        it = items.get(sid)
        if it is None:
            it = items[sid] = {"sid": sid, "desc": ""}
            order.append(sid)
        if len(text) > len(it["desc"]):
            it["desc"] = text
    out = []
    for sid in order:
        it = items[sid]
        if not it["desc"]:
            continue
        i = html.find(f"/a/{sid}")
        it["lang"] = guess_lang(it["desc"] + " " + _strip(html[i:i + 1200]))
        out.append(it)
        if len(out) >= limit:
            break
    return out


async def download_sub(cli: httpx.AsyncClient, sid: str) -> bytes | None:
    """走完 prepare → 中间页 → down → 直链 四跳，返回字幕文件内容（可能是 zip）。"""
    try:
        r = await cli.post(f"{BASE}/api/sub/prepare-download", json={"sid": sid},
                           headers={"Referer": f"{BASE}/a/{sid}"})
        j = r.json()
        url = j.get("url") if j.get("success") else None
        if not url or not str(url).startswith("/down/"):
            return None
        await cli.get(BASE + url)  # 中间页：建立下载会话
        r2 = await cli.post(f"{BASE}/api/sub/down", json={"sid": sid},
                            headers={"Referer": BASE + url})
        j2 = r2.json()
        if not (j2.get("success") and j2.get("pass") and j2.get("url")):
            return None
        r3 = await cli.get(j2["url"], headers={"Referer": BASE + url})
        if r3.status_code == 200 and len(r3.content) >= 20:
            return r3.content
    except Exception:
        return None
    return None


_DETAIL_ID_RE = re.compile(r"href='/d/(\d+)'")
_DETAIL_ITEM_RE = re.compile(
    r"<a href='/d/(\d+)'>\s*<div class=\"pics\">\s*<img[^>]+alt=\"([^\"]*)\"", re.S)
_PLOT_RE = re.compile(r"<b>剧情</b>：(.*?)<br>", re.S)


def _pick_detail_id(html: str, keyword: str) -> str | None:
    """搜索结果里挑详情页：优先条目标题（alt 首段中文名）与关键词完全一致，
    否则退回第一条。避免搜「灌篮高手」却命中「大灌篮」。"""
    first = None
    for m in _DETAIL_ITEM_RE.finditer(html):
        did, alt = m.group(1), _html.unescape(m.group(2)).strip()
        if first is None:
            first = did
        if alt == keyword or alt.split(" ")[0] == keyword:
            return did
    if first:
        return first
    m = _DETAIL_ID_RE.search(html)
    return m.group(1) if m else None


async def search_overview(keyword: str) -> str | None:
    """/d/ 影片页的「剧情」字段（完整简介，subhd 条目与豆瓣同源）。网络失败返回 None。"""
    try:
        async with client() as cli:
            r = await cli.get(f"{BASE}/search/{keyword}")
            did = _pick_detail_id(r.text, keyword)
            if not did:
                return None
            r2 = await cli.get(f"{BASE}/d/{did}")
        pm = _PLOT_RE.search(r2.text)
        if not pm:
            return None
        # 剧情是中英文混排正文：去标签不补空格（避免「的<b>赤木</b>刚宪」变「的 赤木 刚宪」）
        txt = re.sub(r"<[^>]+>", "", pm.group(1))
        txt = _html.unescape(re.sub(r"\s+", " ", txt)).strip()
        return txt or None
    except Exception:
        return None


async def search_posters(keyword: str, limit: int = 8) -> list:
    """搜索结果页附带的豆瓣海报直链（600px webp）。网络失败返回 []。"""
    try:
        async with client() as cli:
            r = await cli.get(f"{BASE}/search/{keyword}")
        urls = []
        for m in _POSTER_RE.finditer(r.text):
            u = m.group(1)
            if u not in urls:
                urls.append(u)
            if len(urls) >= limit:
                break
        return urls
    except Exception:
        return []
