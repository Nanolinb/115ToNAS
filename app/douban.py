"""豆瓣海报源（公开搜索页，国内直连）。

反爬实测：
- 搜索页需先访问首页种 bid cookie，否则可能拿到验证页；
- 条目页（movie.douban.com/subject/）会被 sec.douban.com 拦截，所以简介只取
  搜索页结果里自带的 <p> 段落（约百字截断版）；
- 图床 img*.doubanio.com 有防盗链（无 Referer 返回 418），
  显示走 /api/imgproxy、服务端下载带 Referer（见 baiduimg._referer_for）；
- 海报取 m_ratio（540px，比 s_ratio 的 270px 更适合电视海报墙）。
"""
import re
import html as _html

import httpx

from .config import UA

_RESULT_RE = re.compile(
    r'<img src="(https://img\d\.doubanio\.com/view/photo/s_ratio_poster/[^"]+)"')
_INTRO_RE = re.compile(r"<p>(.*?)</p>", re.S)


async def _search_page(cli: httpx.AsyncClient, keyword: str) -> str:
    await cli.get("https://www.douban.com/")  # 种 bid cookie
    r = await cli.get(f"https://www.douban.com/search?cat=1002&q={keyword}",
                      headers={"Referer": "https://www.douban.com/"})
    return r.text


async def search_overview(keyword: str, year: int | None = None) -> str | None:
    """搜索页结果自带的简介段（截断版）。优先取年份一致的条目，其次第一条。
    网络/反爬失败返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": UA}) as cli:
            text = await _search_page(cli, keyword)
        fallback = None
        for block in text.split('<div class="result">')[1:]:
            m = _INTRO_RE.search(block)
            if not m:
                continue
            txt = _html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip()
            if not txt:
                continue
            if year and str(year) in block:
                return txt
            if fallback is None:
                fallback = txt
        return fallback
    except Exception:
        return None


async def search_posters(keyword: str, limit: int = 6) -> list:
    """豆瓣电影搜索页刮海报直链（m_ratio）。网络/反爬失败返回 []。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": UA}) as cli:
            text = await _search_page(cli, keyword)
        urls = []
        for m in _RESULT_RE.finditer(text):
            u = m.group(1).replace("s_ratio_poster", "m_ratio_poster")
            if u not in urls:
                urls.append(u)
            if len(urls) >= limit:
                break
        return urls
    except Exception:
        return []
