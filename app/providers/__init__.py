"""网盘来源注册表。

业务层只依赖 Provider 协议；115 是默认实现，后续接 WebDAV/阿里云盘等
不再把来源判断散落到下载器和路由里。
"""
from .base import CloudProvider
from .cloud115_provider import Cloud115Provider

_providers: dict[str, CloudProvider] = {
    "115": Cloud115Provider(),
}


def get_provider(name: str = "115") -> CloudProvider:
    try:
        return _providers[name]
    except KeyError as exc:
        raise ValueError(f"未知网盘来源: {name}") from exc


def provider_names() -> list[str]:
    return sorted(_providers)
