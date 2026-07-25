"""TMDB 元数据：搜索匹配 + 详情 + 海报本地缓存。未配置 API key 时全部静默跳过。"""
import asyncio

import httpx

from . import db
from .config import POSTER_DIR

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"


def api_key() -> str | None:
    key = db.get_secret("tmdb_key", "").strip()
    return key or None


async def _get(client: httpx.AsyncClient, path: str, **params):
    params["api_key"] = api_key()
    params.setdefault("language", "zh-CN")
    r = await client.get(f"{BASE}{path}", params=params)
    r.raise_for_status()
    return r.json()


async def match(title: str, year: int | None, kind: str) -> dict | None:
    """kind: 'movie' | 'tv'。返回标准化元数据或 None。"""
    if not api_key() or not title:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            params = {"query": title}
            if year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            data = await _get(client, f"/search/{kind}", **params)
            results = data.get("results") or []
            if not results and year:  # 去掉年份再试一次
                data = await _get(client, f"/search/{kind}", query=title)
                results = data.get("results") or []
            if not results:
                return None
            best = results[0]
            detail = await _get(client, f"/{kind}/{best['id']}")
            poster = await _cache_image(client, detail.get("poster_path"), "w500")
            backdrop = await _cache_image(client, detail.get("backdrop_path"), "w780")
            date = detail.get("release_date") or detail.get("first_air_date") or ""
            return {
                "tmdb_id": detail.get("id"),
                "title": detail.get("title") or detail.get("name") or title,
                "original_title": detail.get("original_title") or detail.get("original_name"),
                "name_cn": detail.get("title") or detail.get("name"),
                "year": int(date[:4]) if date[:4].isdigit() else year,
                "poster": poster,
                "backdrop": backdrop,
                "overview": (detail.get("overview") or "")[:2000],
                "genres": ",".join(g["name"] for g in detail.get("genres", [])),
                "rating": round(float(detail.get("vote_average") or 0), 1),
            }
    except Exception:
        return None


async def match_by_id(tmdb_id: int, kind: str) -> dict | None:
    if not api_key():
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            detail = await _get(client, f"/{kind}/{tmdb_id}")
            poster = await _cache_image(client, detail.get("poster_path"), "w500")
            backdrop = await _cache_image(client, detail.get("backdrop_path"), "w780")
            date = detail.get("release_date") or detail.get("first_air_date") or ""
            return {
                "tmdb_id": detail.get("id"),
                "title": detail.get("title") or detail.get("name"),
                "original_title": detail.get("original_title") or detail.get("original_name"),
                "name_cn": detail.get("title") or detail.get("name"),
                "year": int(date[:4]) if date[:4].isdigit() else None,
                "poster": poster,
                "backdrop": backdrop,
                "overview": (detail.get("overview") or "")[:2000],
                "genres": ",".join(g["name"] for g in detail.get("genres", [])),
                "rating": round(float(detail.get("vote_average") or 0), 1),
            }
    except Exception:
        return None


async def search_candidates(title: str, kind: str) -> list:
    """手动匹配用：返回前 8 条候选。"""
    if not api_key() or not title:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            data = await _get(client, f"/search/{kind}", query=title)
            out = []
            for r in (data.get("results") or [])[:8]:
                date = r.get("release_date") or r.get("first_air_date") or ""
                out.append({
                    "tmdb_id": r.get("id"),
                    "title": r.get("title") or r.get("name"),
                    "original_title": r.get("original_title") or r.get("original_name"),
                    "year": date[:4],
                    "rating": round(float(r.get("vote_average") or 0), 1),
                    "poster_url": f"{IMG}/w185{r['poster_path']}" if r.get("poster_path") else None,
                    "overview": (r.get("overview") or "")[:300],
                })
            return out
    except Exception:
        return []


async def _cache_image(client: httpx.AsyncClient, path: str | None, size: str) -> str | None:
    if not path:
        return None
    fname = f"{path.strip('/').replace('/', '_')}"
    dest = POSTER_DIR / fname
    if dest.exists():
        return fname
    try:
        r = await client.get(f"{IMG}/{size}{path}")
        if r.status_code == 200 and len(r.content) > 1000:
            dest.write_bytes(r.content)
            return fname
    except Exception:
        pass
    return None
