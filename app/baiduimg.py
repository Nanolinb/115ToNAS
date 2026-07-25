"""百度图片海报源：国内可直连。
两个用途：
1. TMDB 不可用/未配置时的封面兜底（扫描时自动尝试）；
2. 详情页「更换封面」手动挑选（搜图候选 → 点击设定）。
"""
import hashlib

import httpx

from . import db
from .config import POSTER_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": UA, "Referer": "https://image.baidu.com/"}


def _referer_for(url: str) -> str:
    """各图床的防盗链 Referer（豆瓣无 Referer 返回 418）。"""
    host = httpx.URL(url).host or ""
    if host.endswith(".doubanio.com"):
        return "https://www.douban.com/"
    if host.endswith(".subhd.me"):
        return "https://subhd.cc/"
    return "https://image.baidu.com/"


async def _clients(timeout: int):
    """直连 + （可选）代理客户端；部分网络下百度图床(img*.baidu.com)被拦截时走代理兜底。"""
    clients = [httpx.AsyncClient(timeout=timeout, headers=_HEADERS)]
    proxy = (db.get_setting("proxy_url", "") or "").strip()
    if proxy:
        try:
            clients.append(httpx.AsyncClient(proxy=proxy, timeout=timeout,
                                             headers=_HEADERS))
        except TypeError:  # httpx < 0.26 用 proxies=
            clients.append(httpx.AsyncClient(proxies=proxy, timeout=timeout,
                                             headers=_HEADERS))
    return clients


async def search_posters(query: str, limit: int = 12) -> list:
    """百度图片搜索，返回图片直链列表（middleURL 优先）。网络瞬断有限重试。"""
    if not query.strip():
        return []
    params = {"tn": "resultjson_com", "ipn": "rj", "ct": "201326592",
              "word": query, "pn": 0, "rn": 30}
    data = None
    clients = await _clients(12)
    try:
        for c in clients:
            for attempt in range(3):
                try:
                    r = await c.get("https://image.baidu.com/search/acjson",
                                    params=params)
                    data = r.json()
                    break
                except Exception:
                    if attempt < 2:
                        import asyncio
                        await asyncio.sleep(1.2 * (attempt + 1))
            if data is not None:
                break
    finally:
        for c in clients:
            await c.aclose()
    if data is None:
        return []
    out = []
    for item in data.get("data") or []:
        u = item.get("middleURL") or item.get("thumbURL")
        if u and u.startswith("http"):
            out.append(u)
        if len(out) >= limit:
            break
    return out


async def fetch_image(url: str) -> tuple | None:
    """imgproxy 用：带 per-host Referer 抓图，成功返回 (内容, content-type)。"""
    try:
        async with httpx.AsyncClient(timeout=15,
                                     headers={"User-Agent": UA}) as c:
            r = await c.get(url, headers={"Referer": _referer_for(url)},
                            follow_redirects=True)
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and "image" in ctype and len(r.content) > 1000:
                return r.content, ctype
    except Exception:
        pass
    return None


async def download_poster(url: str) -> str | None:
    """下载封面到本地缓存目录（直连失败走代理兜底，各 3 次重试），成功返回文件名。"""
    clients = await _clients(15)
    try:
        for c in clients:
            for attempt in range(3):
                try:
                    r = await c.get(url, headers={"Referer": _referer_for(url)},
                                    follow_redirects=True)
                    ctype = r.headers.get("content-type", "")
                    if r.status_code == 200 and len(r.content) > 5000 and \
                            ("image" in ctype or url.lower().endswith(
                                (".jpg", ".jpeg", ".png", ".webp"))):
                        fname = "bd_" + hashlib.md5(url.encode()).hexdigest() + ".jpg"
                        (POSTER_DIR / fname).write_bytes(r.content)
                        return fname
                    break  # 拿到响应但不合格，换源图，不重试
                except Exception:
                    if attempt < 2:
                        import asyncio
                        await asyncio.sleep(1.2 * (attempt + 1))
    finally:
        for c in clients:
            await c.aclose()
    return None


async def poster_for(title: str, year: int | None = None) -> str | None:
    """按片名自动抓一张封面（扫描兜底用），取前几个候选中第一个能下载的。"""
    q = f"{title} {year or ''} 电影海报".replace("  ", " ").strip()
    for url in await search_posters(q, 4):
        fname = await download_poster(url)
        if fname:
            return fname
    return None
