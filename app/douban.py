"""豆瓣海报源（公开搜索页，国内直连）。

反爬实测：
- 搜索页需先访问首页种 bid cookie，否则可能拿到验证页；
- 图床 img*.doubanio.com 有防盗链（无 Referer 返回 418），
  显示走 /api/imgproxy、服务端下载带 Referer（见 baiduimg._referer_for）；
- 海报取 m_ratio（540px，比 s_ratio 的 270px 更适合电视海报墙）。
"""
import re

import httpx

from .config import UA

_RESULT_RE = re.compile(
    r'<img src="(https://img\d\.doubanio\.com/view/photo/s_ratio_poster/[^"]+)"')


async def search_posters(keyword: str, limit: int = 6) -> list:
    """豆瓣电影搜索页刮海报直链（m_ratio）。网络/反爬失败返回 []。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": UA}) as cli:
            await cli.get("https://www.douban.com/")  # 种 bid cookie
            r = await cli.get(f"https://www.douban.com/search?cat=1002&q={keyword}",
                              headers={"Referer": "https://www.douban.com/"})
        urls = []
        for m in _RESULT_RE.finditer(r.text):
            u = m.group(1).replace("s_ratio_poster", "m_ratio_poster")
            if u not in urls:
                urls.append(u)
            if len(urls) >= limit:
                break
        return urls
    except Exception:
        return []
