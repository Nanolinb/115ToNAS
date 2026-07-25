"""115 网盘封装：二维码登录（本地生成 QR 图）、文件浏览/搜索、取下载直链。

底层用 p115client（同步），通过 asyncio.to_thread 调用，避免阻塞事件循环。
"""
import asyncio
import io
import time

import httpx

from . import db
from .config import COOKIES_PATH, UA

QR_API = "https://qrcodeapi.115.com"


class CloudError(Exception):
    pass


async def _req_json(method: str, url: str, retries: int = 3, **kw) -> dict:
    """115 API 请求：针对网络抖动做有限重试。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=15) as h:
                r = await h.request(method, url, **kw)
                return r.json()
        except Exception as e:  # 连接超时/重置等瞬断，重试
            last = e
            if attempt < retries - 1:
                await asyncio.sleep(1.2 * (attempt + 1))
    raise CloudError(f"网络请求失败（已重试 {retries} 次）: {type(last).__name__}")


class Cloud115:
    def __init__(self):
        self._client = None
        self._pending_qr: dict = {}  # uid -> {time, sign, created}
        self._load_client()

    # ---------- 登录 ----------

    def _load_client(self):
        self._client = None
        cookie = db.get_secret("115_cookie", "")
        if not cookie and COOKIES_PATH.exists():
            # 旧版本明文 cookie 文件：迁移进加密库后删除
            legacy = COOKIES_PATH.read_text().strip()
            if legacy:
                db.set_secret("115_cookie", legacy)
                cookie = legacy
            COOKIES_PATH.unlink(missing_ok=True)
        if cookie:
            try:
                from p115client import P115Client
                # console_qrcode=False 防止在容器里尝试弹二维码
                self._client = P115Client(cookie, console_qrcode=False)
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
        """发起一次扫码登录，返回 {uid, qr_png(base64 不需要，直接 bytes 由路由返回)}。"""
        data = (await _req_json("GET", f"{QR_API}/api/1.0/web/1.0/token/", retries=5)).get("data") or {}
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
        if not pend.get("confirmed"):
            try:
                payload = await _req_json(
                    "GET", f"{QR_API}/get/status/", retries=2,
                    params={"uid": uid, "time": pend["time"], "sign": pend["sign"]})
            except CloudError:
                return {"status": "waiting"}  # 轮询遇网络抖动：保持等待，下轮再试
            status = (payload.get("data") or {}).get("status")
            if status == 2:
                pend["confirmed"] = True  # 记住已确认，后续专注完成登录
            elif status == 1:
                return {"status": "scanned"}
            elif status == 0:
                return {"status": "waiting"}
            else:
                return {"status": "expired"}
        # 已确认：完成登录。失败（网络瞬断等）下轮继续重试，不丢状态
        try:
            await self._complete_login(uid)
            return {"status": "done"}
        except CloudError:
            pend["fail_count"] = pend.get("fail_count", 0) + 1
            if pend["fail_count"] > 15:
                self._pending_qr.pop(uid, None)
                return {"status": "expired"}
            return {"status": "scanned"}

    async def _complete_login(self, uid: str):
        # 注意：app="web" 极易触发 115 的「IP登录异常」封禁，改用 alipaymini
        # 通道（p115client 官方默认）；URL 末尾斜杠与请求体格式对齐官方实现
        payload = await _req_json(
            "POST", f"{QR_API}/app/1.0/alipaymini/1.0/login/qrcode/", retries=5,
            data={"account": uid})
        if not payload.get("state"):
            msg = (payload.get("error") or payload.get("msg")
                   or payload.get("message") or "扫码登录失败")
            print(f"[cloud115] login_qrcode 失败: {msg}")
            raise CloudError(str(msg))
        cookie = (payload.get("data") or {}).get("cookie") or {}
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie.items())
        db.set_secret("115_cookie", cookie_str)  # 加密存储，不落明文
        self._pending_qr.pop(uid, None)
        self._load_client()

    def logout(self):
        db.set_secret("115_cookie", "")
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

    SORT_MAP = {
        "time_desc": ("user_ptime", 0), "time_asc": ("user_ptime", 1),
        "name_asc": ("file_name", 1), "name_desc": ("file_name", 0),
        "size_desc": ("file_size", 0), "size_asc": ("file_size", 1),
    }

    @staticmethod
    def _name_key(name: str):
        """名称排序键：中文按拼音序（GBK 编码天然按拼音排列），ASCII 不区分大小写。"""
        n = name.lower()
        try:
            return n.encode("gbk")
        except UnicodeEncodeError:
            return n.encode("utf-8")

    def _sort_items(self, items: list, sort: str) -> list:
        """本地排序兜底：目录恒在文件前，组内按选定规则排，不依赖服务端排序参数。"""
        if sort in ("name_asc", "name_desc"):
            key = lambda x: self._name_key(x["name"])
        elif sort in ("size_desc", "size_asc"):
            key = lambda x: x["size"]
        else:  # time_desc / time_asc；mtime 可能是时间戳或 "YYYY-MM-DD HH:MM"
            def key(x):
                t = str(x.get("mtime") or "")
                return t.zfill(20) if t.isdigit() else t
        reverse = sort.endswith("_desc")
        dirs = sorted((i for i in items if i["is_dir"]), key=key, reverse=reverse)
        files = sorted((i for i in items if not i["is_dir"]), key=key, reverse=reverse)
        return dirs + files

    async def _fs_call(self, fn, retries: int = 3):
        """p115client 同步调用：转线程 + 网络瞬断重试。"""
        last: Exception | None = None
        for attempt in range(retries):
            try:
                return await asyncio.to_thread(fn)
            except CloudError:
                raise
            except Exception as e:
                last = e
                if attempt < retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
        raise CloudError(f"网络请求失败（已重试 {retries} 次）: {type(last).__name__}")

    async def list_files(self, cid: str = "0", offset: int = 0, limit: int = 200,
                         sort: str = "time_desc") -> dict:
        client = self._require_client()
        order, asc = self.SORT_MAP.get(sort, self.SORT_MAP["time_desc"])

        def _call():
            resp = client.fs_files({
                "cid": cid, "offset": offset, "limit": limit,
                "show_dir": 1, "o": order, "asc": asc,
            })
            if not resp.get("state"):
                raise CloudError(str(resp.get("error") or "列出文件失败"))
            return resp

        resp = await self._fs_call(_call)
        items = [self._normalize(it) for it in resp.get("data", [])]
        items = self._sort_items(items, sort)
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

        resp = await self._fs_call(_call)
        return [self._normalize(it) for it in resp.get("data", [])]

    async def get_download_url(self, pickcode: str) -> str:
        client = self._require_client()

        def _call():
            # 直链签名与取链时的 User-Agent 绑定（URL 参数 f=1），
            # 必须用 user_agent 形参传入，保证下载时用同一个 UA，
            # 否则 CDN 返回 403 invalid signature
            return str(client.download_url(pickcode, app="android",
                                           user_agent=UA))

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

    async def iter_media_files(self, cid: str, root_name: str, depth: int = 0,
                               max_depth: int = 4, rel: str = ""):
        """递归遍历文件夹：视频 + 同目录字幕一起产出，
        每项附带 rel（相对目录，含根文件夹名），下载时还原 115 的目录结构。"""
        from .parser import is_subtitle, is_video
        base = f"{rel}/{root_name}" if rel else root_name
        offset = 0
        while True:
            page = await self.list_files(cid, offset=offset, limit=200)
            for it in page["items"]:
                if it["is_dir"]:
                    if depth < max_depth:
                        async for sub in self.iter_media_files(
                                it["id"], it["name"], depth + 1, max_depth, base):
                            yield sub
                elif is_video(it["name"]) or is_subtitle(it["name"]):
                    yield {**it, "rel": base}
            if offset + 200 >= page["count"]:
                break
            offset += 200

    async def list_sibling_subs(self, cid: str) -> list:
        """列出指定目录下全部字幕文件（用于单选视频时寻找同片字幕）。"""
        from .parser import is_subtitle
        subs, offset = [], 0
        while True:
            page = await self.list_files(cid, offset=offset, limit=200)
            for it in page["items"]:
                if not it["is_dir"] and is_subtitle(it["name"]):
                    subs.append(it)
            if offset + 200 >= page["count"]:
                break
            offset += 200
        return subs


cloud = Cloud115()
