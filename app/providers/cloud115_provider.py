"""115 Provider 适配器。"""
import asyncio

from ..cloud115 import cloud


class Cloud115Provider:
    name = "115"

    async def is_logged_in(self) -> bool:
        # p115client 的登录检查是同步网络调用，必须移出事件循环。
        return await asyncio.to_thread(cloud.is_logged_in)

    async def get_download_url(self, remote_id: str) -> str:
        return await cloud.get_download_url(remote_id)
