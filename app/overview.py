"""简介兜底：TMDB 刮不到 overview 时，去 subhd.cc（/d/ 页完整剧情）/
豆瓣搜索页（结果自带截断简介）找同名影片摘录。

两个入口：
- fetch_overview：扫描/重新匹配时同步补缺；
- schedule_fill：观影端 /api/library 发现缺简介条目时后台批量补，
  下一轮刷新即见，不拖慢海报墙首屏。
"""
import asyncio

from . import db, douban, subhd

MAX_LEN = 2000

_fill_task: asyncio.Task | None = None


async def fetch_overview(title: str, year: int | None = None) -> str | None:
    """subhd（完整剧情）优先，豆瓣（截断简介）兜底。都失败返回 None。

    NAS 出口到这两个站偶发超时，失败时隔 3 秒再试一轮。"""
    if not title:
        return None
    for attempt in (0, 1):
        o = await subhd.search_overview(title)
        if not o:
            o = await douban.search_overview(title, year)
        o = (o or "").strip()
        if o:
            return o[:MAX_LEN]
        if attempt == 0:
            await asyncio.sleep(3)
    return None


def schedule_fill(media_ids: list):
    """后台给缺简介的条目补简介。一次只跑一轮，避免对站点造成压力。"""
    global _fill_task
    if _fill_task and not _fill_task.done():
        return
    _fill_task = asyncio.create_task(_fill(media_ids))


async def _fill(media_ids: list):
    for mid in media_ids:
        row = db.one("SELECT id, type, title, name_cn, year, tmdb_id FROM media "
                     "WHERE id=? AND (overview IS NULL OR overview='')", (mid,))
        if not row:
            continue
        o = await fetch_overview(row["name_cn"] or row["title"], row["year"])
        if not o:
            continue
        print(f"[overview] 简介已补: {row['name_cn'] or row['title']}")
        # 剧集：同剧各集共享同一简介（限定 episode，避免与电影 tmdb_id 撞号）
        if row["type"] == "episode" and row["tmdb_id"]:
            db.exe("UPDATE media SET overview=? WHERE tmdb_id=? AND type='episode'",
                   (o, row["tmdb_id"]))
        else:
            db.exe("UPDATE media SET overview=? WHERE id=?", (o, mid))
