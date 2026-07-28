"""多网盘接入的最小协议。"""
from typing import Protocol


class CloudProvider(Protocol):
    name: str

    async def is_logged_in(self) -> bool:
        """账号凭据当前是否可用。"""

    async def get_download_url(self, remote_id: str) -> str:
        """返回短期下载地址。"""
