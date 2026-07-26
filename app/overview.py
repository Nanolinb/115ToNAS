"""元数据兜底：TMDB 刮不到简介/题材/年份时，去 subhd.cc（/d/ 页剧情/类型/
年代一把抓）/ 豆瓣搜索页（结果自带截断简介）找同名影片信息。

两个入口：
- fetch_meta / fetch_overview：扫描/重新匹配时同步补缺；
- schedule_fill：观影端 /api/library 发现缺简介/题材/年份条目时后台批量补，
  下一轮刷新即见，不拖慢海报墙首屏。
"""
import asyncio

from . import db, douban, subhd

MAX_LEN = 2000

_fill_task: asyncio.Task | None = None


async def fetch_meta(title: str, year: int | None = None) -> dict | None:
    """subhd 详情页一把抓（简介+年份+题材），豆瓣只补简介。都失败返回 None。

    NAS 出口到这两个站偶发超时，失败时隔 3 秒再试一轮。"""
    if not title:
        return None
    for attempt in (0, 1):
        meta = await subhd.search_meta(title)
        if meta:
            o = meta.get("overview")
            if not o:
                o = await douban.search_overview(title, year)
                if o:
                    meta["overview"] = o
            if meta.get("overview"):
                meta["overview"] = meta["overview"][:MAX_LEN]
            return meta
        o = await douban.search_overview(title, year)
        if o:
            return {"overview": o[:MAX_LEN]}
        if attempt == 0:
            await asyncio.sleep(3)
    return None


async def fetch_overview(title: str, year: int | None = None) -> str | None:
    """只要简介时用。subhd（完整剧情）优先，豆瓣（截断简介）兜底。"""
    meta = await fetch_meta(title, year)
    return (meta or {}).get("overview")


def schedule_fill(media_ids: list):
    """后台给缺简介/题材/年份的条目补全。一次只跑一轮，避免对站点造成压力。"""
    global _fill_task
    if _fill_task and not _fill_task.done():
        return
    _fill_task = asyncio.create_task(_fill(media_ids))


async def _fill(media_ids: list):
    for mid in media_ids:
        row = db.one(
            "SELECT id, type, title, name_cn, year, tmdb_id, overview, genres "
            "FROM media WHERE id=? AND ((overview IS NULL OR overview='') "
            "OR (genres IS NULL OR genres='') OR year IS NULL)", (mid,))
        if not row:
            continue
        m = await fetch_meta(row["name_cn"] or row["title"], row["year"])
        if not m:
            continue
        sets = {}
        if m.get("overview") and not (row["overview"] or "").strip():
            sets["overview"] = m["overview"]
        if m.get("genres") and not (row["genres"] or "").strip():
            sets["genres"] = m["genres"]
        if m.get("year") and not row["year"]:
            sets["year"] = m["year"]
        if not sets:
            continue
        print(f"[overview] 信息已补({','.join(sets)}): {row['name_cn'] or row['title']}")
        # 剧集：同剧各集共享（限定 episode，避免与电影 tmdb_id 撞号；
        # 逐字段只补空，不覆盖其他集已有的值）
        if row["type"] == "episode" and row["tmdb_id"]:
            for k, v in sets.items():
                cond = "year IS NULL" if k == "year" else f"({k} IS NULL OR {k}='')"
                db.exe(f"UPDATE media SET {k}=? WHERE tmdb_id=? AND type='episode' "
                       f"AND {cond}", (v, row["tmdb_id"]))
        else:
            cols = ",".join(f"{k}=?" for k in sets)
            db.exe(f"UPDATE media SET {cols} WHERE id=?",
                   list(sets.values()) + [mid])
