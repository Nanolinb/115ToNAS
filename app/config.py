"""全局配置与常量。资源敏感：只做轻量目录初始化。"""
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/media"))

DB_PATH = CONFIG_DIR / "data.db"
COOKIES_PATH = CONFIG_DIR / "115-cookies.txt"
CACHE_DIR = CONFIG_DIR / "cache"
POSTER_DIR = CACHE_DIR / "posters"
SUB_TMP_DIR = CACHE_DIR / "subtitles"

# 115 下载时使用的 UA（取链与下载需保持一致）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

APP_PORT = int(os.environ.get("APP_PORT", "8115"))

for _d in (CONFIG_DIR, CACHE_DIR, POSTER_DIR, SUB_TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)
