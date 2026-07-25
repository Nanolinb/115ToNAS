"""TMDB 元数据：搜索匹配 + 详情 + 海报本地缓存。未配置 API key 时全部静默跳过。

网络策略（国内适配）：
- 每次请求先试直连；直连失败且设置了代理（proxy_url）时自动回退代理
- 连通结果记录在 settings.tmdb_net（direct/proxy/fail）+ tmdb_net_at（时间戳），
  管理端设置页据此提示；下载完成自动刮削时天然触发，无需手动检测
"""
import time

import httpx

from . import db
from .config import POSTER_DIR

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"


def api_key() -> str | None:
    key = db.get_secret("tmdb_key", "").strip()
    return key or None


def _proxy() -> str:
    return (db.get_setting("proxy_url", "") or "").strip()


def _record(mode: str):
    """记录 TMDB 连通方式：direct / proxy / fail。值不变时不写库。"""
    if db.get_setting("tmdb_net") != mode:
        db.set_setting("tmdb_net", mode)
    db.set_setting("tmdb_net_at", str(int(time.time())))


async def _clients(timeout: int):
    """直连客户端 + （可选）代理客户端，调用方负责关闭。"""
    clients = [httpx.AsyncClient(timeout=timeout)]
    proxy = _proxy()
    if proxy:
        try:
            clients.append(httpx.AsyncClient(proxy=proxy, timeout=timeout))
        except TypeError:  # httpx < 0.26 用 proxies=
            clients.append(httpx.AsyncClient(proxies=proxy, timeout=timeout))
    return clients


async def _get(path: str, **params):
    """GET TMDB API：直连优先，代理兜底，记录连通状态。"""
    params["api_key"] = api_key()
    params.setdefault("language", "zh-CN")
    last: Exception | None = None
    clients = await _clients(15)
    try:
        for i, client in enumerate(clients):
            try:
                r = await client.get(f"{BASE}{path}", params=params)
                r.raise_for_status()
                _record("direct" if i == 0 else "proxy")
                return r.json()
            except httpx.HTTPStatusError:
                # 拿到 HTTP 响应 = 网络已通（如 401 key 错误），不应记为网络失败
                _record("direct" if i == 0 else "proxy")
                raise
            except Exception as e:
                last = e
    finally:
        for client in clients:
            await client.aclose()
    _record("fail")
    raise last


async def _cache_image(path: str | None, size: str) -> str | None:
    if not path:
        return None
    fname = f"{path.strip('/').replace('/', '_')}"
    dest = POSTER_DIR / fname
    if dest.exists():
        return fname
    clients = await _clients(15)
    try:
        for i, client in enumerate(clients):
            try:
                r = await client.get(f"{IMG}/{size}{path}")
                if r.status_code == 200 and len(r.content) > 1000:
                    dest.write_bytes(r.content)
                    _record("direct" if i == 0 else "proxy")
                    return fname
            except Exception:
                continue
    finally:
        for client in clients:
            await client.aclose()
    return None


def _normalize(detail: dict, fallback_title: str, fallback_year) -> dict:
    date = detail.get("release_date") or detail.get("first_air_date") or ""
    return {
        "tmdb_id": detail.get("id"),
        "title": detail.get("title") or detail.get("name") or fallback_title,
        "original_title": detail.get("original_title") or detail.get("original_name"),
        "name_cn": detail.get("title") or detail.get("name"),
        "year": int(date[:4]) if date[:4].isdigit() else fallback_year,
        "poster": detail.get("_poster"),
        "backdrop": detail.get("_backdrop"),
        "overview": (detail.get("overview") or "")[:2000],
        "genres": ",".join(g["name"] for g in detail.get("genres", [])),
        "rating": round(float(detail.get("vote_average") or 0), 1),
    }


async def match(title: str, year: int | None, kind: str) -> dict | None:
    """kind: 'movie' | 'tv'。返回标准化元数据或 None。"""
    if not api_key() or not title:
        return None
    try:
        params = {"query": title}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        data = await _get(f"/search/{kind}", **params)
        results = data.get("results") or []
        if not results and year:  # 去掉年份再试一次
            data = await _get(f"/search/{kind}", query=title)
            results = data.get("results") or []
        if not results:
            return None
        detail = await _get(f"/{kind}/{results[0]['id']}")
        detail["_poster"] = await _cache_image(detail.get("poster_path"), "w500")
        detail["_backdrop"] = await _cache_image(detail.get("backdrop_path"), "w780")
        return _normalize(detail, title, year)
    except Exception:
        return None


async def match_by_id(tmdb_id: int, kind: str) -> dict | None:
    if not api_key():
        return None
    try:
        detail = await _get(f"/{kind}/{tmdb_id}")
        detail["_poster"] = await _cache_image(detail.get("poster_path"), "w500")
        detail["_backdrop"] = await _cache_image(detail.get("backdrop_path"), "w780")
        return _normalize(detail, None, None)
    except Exception:
        return None


async def search_candidates(title: str, kind: str) -> list:
    """手动匹配用：返回前 8 条候选。"""
    if not api_key() or not title:
        return []
    try:
        data = await _get(f"/search/{kind}", query=title)
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
