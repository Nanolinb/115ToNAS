"""115 网盘封装：二维码登录（本地生成 QR 图）、文件浏览/搜索、取下载直链。

底层用 p115client（同步），通过 asyncio.to_thread 调用，避免阻塞事件循环。
"""
import asyncio
import io
import time

import httpx

from .config import COOKIES_PATH, UA

QR_API = "https://qrcodeapi.115.com"


class CloudError(Exception):
    pass


class Cloud115:
    def __init__(self):
        self._client = None
        self._pending_qr: dict = {}  # uid -> {time, sign, created}
        self._load_client()

    # ---------- 登录 ----------

    def _load_client(self):
        self._client = None
        if COOKIES_PATH.exists() and COOKIES_PATH.read_text().strip():
            try:
                from p115client import P115Client
                # console_qrcode=False 防止在容器里尝试弹二维码
                self._client = P115Client(COOKIES_PATH, console_qrcode=False)
            except Exception:
                self._client = None

    def is_logged_in(self) -> bool:
        if not self._client:
            return False
        try:
            resp = self._client.fs_files({"cid": 0, "limit": 1, "offset": 0})
            return bool(resp.get("state"))
        except Exception:
            return False

    async def new_qrcode(self) -> dict:
        """发起一次扫码登录，返回 {uid}，PNG 由 qr_png() 提供。"""
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.get(f"{QR_API}/api/1.0/web/1.0/token/")
            data = r.json().get("data") or {}
        uid, ts, sign = data.get("uid"), data.get("time"), data.get("sign")
        content = data.get("qrcode")
        if not (uid and content):
            raise CloudError("获取登录二维码失败")
        self._pending_qr[uid] = {"time": ts, "sign": sign, "created": time.time()}
        # 本地生成二维码 PNG，无需再请求 115
        import qrcode
        img = qrcode.make(content)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self._pending_qr[uid]["png"] = buf.getvalue()
        return {"uid": uid}

    def qr_png(self, uid: str) -> bytes | None:
        pend = self._pending_qr.get(uid)
        return pend.get("png") if pend else None

    async def poll_qrcode(self, uid: str) -> dict:
        """返回 {status: waiting|scanned|done|expired}"""
        pend = self._pending_qr.get(uid)
        if not pend:
            return {"status": "expired"}
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.get(f"{QR_API}/get/status/",
                            params={"uid": uid, "time": pend["time"], "sign": pend["sign"]})
            status = (r.json().get("data") or {}).get("status")
        if status == 0:
            return {"status": "waiting"}
        if status == 1:
            return {"status": "scanned"}
        if status == 2:
            await self._complete_login(uid)
            return {"status": "done"}
        return {"status": "expired"}

    async def _complete_login(self, uid: str):
        async with httpx.AsyncClient(timeout=15) as h:
            r = await h.post(f"{QR_API}/app/1.0/web/1.0/login/qrcode",
                             params={"account": uid, "app": "web"})
            payload = r.json()
        if not payload.get("state"):
            raise CloudError("扫码登录失败")
        cookie = (payload.get("data") or {}).get("cookie") or {}
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie.items())
        COOKIES_PATH.write_text(cookie_str)
        self._pending_qr.pop(uid, None)
        self._load_client()

    def logout(self):
        COOKIES_PATH.unlink(missing_ok=True)
        self._client = None

    # ---------- 文件操作 ----------

    def _require_client(self):
        if not self._client:
            raise CloudError("115 未登录")
        return self._client

    @staticmethod
    def _normalize(item: dict) -> dict:
        is_dir = "fid" not in item
        return {
            "id": str(item.get("cid") if is_dir else item.get("fid")),
            "name": item.get("n", ""),
            "is_dir": is_dir,
            "size": int(item.get("s") or 0),
            "pickcode": item.get("pc", ""),
            "mtime": item.get("t", ""),
        }

    async def list_files(self, cid: str = "0", offset: int = 0, limit: int = 200) -> dict:
        client = self._require_client()

        def _call():
            resp = client.fs_files({
                "cid": cid, "offset": offset, "limit": limit,
                "show_dir": 1, "o": "user_ptime", "asc": 0,
            })
            if not resp.get("state"):
                raise CloudError(str(resp.get("error") or "列出文件失败"))
            return resp

        resp = await asyncio.to_thread(_call)
        items = [self._normalize(it) for it in resp.get("data", [])]
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        crumbs = [{"cid": str(p.get("cid", "0")), "name": p.get("name", "")}
                  for p in (resp.get("path") or [])]
        return {"items": items, "count": int(resp.get("count") or len(items)),
                "path": crumbs}

    async def search(self, keyword: str, limit: int = 100) -> list:
        client = self._require_client()

        def _call():
            resp = client.fs_search({
                "search_value": keyword, "limit": limit, "offset": 0,
            })
            if not resp.get("state"):
                raise CloudError(str(resp.get("error") or "搜索失败"))
            return resp

        resp = await asyncio.to_thread(_call)
        return [self._normalize(it) for it in resp.get("data", [])]

    async def get_download_url(self, pickcode: str) -> str:
        client = self._require_client()

        def _call():
            return str(client.download_url(pickcode, app="android",
                                           headers={"User-Agent": UA}))

        try:
            return await asyncio.to_thread(_call)
        except Exception as e:
            raise CloudError(f"获取下载链接失败: {e}")

    async def iter_video_files(self, cid: str, depth: int = 0, max_depth: int = 4):
        """递归遍历文件夹内的视频文件（用于整文件夹加入下载）。"""
        from .parser import is_video
        offset = 0
        while True:
            page = await self.list_files(cid, offset=offset, limit=200)
            for it in page["items"]:
                if it["is_dir"]:
                    if depth < max_depth:
                        async for sub in self.iter_video_files(it["id"], depth + 1, max_depth):
                            yield sub
                elif is_video(it["name"]):
                    yield it
            if offset + 200 >= page["count"]:
                break
            offset += 200


cloud = Cloud115()
