"""视频文件名解析：优先 guessit（对外语发行版命名很准），失败时退回正则。"""
import re
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts",
              ".mpg", ".mpeg", ".rmvb", ".rm", ".vob", ".webm", ".f4v", ".3gp"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

try:
    import guessit
    _HAS_GUESSIT = True
except Exception:
    _HAS_GUESSIT = False

_EP_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})"),
    re.compile(r"[Ee][Pp]?(\d{1,3})"),
    re.compile(r"第\s*(\d{1,3})\s*[集话回]"),
]
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_TAG_RE = re.compile(
    r"(2160p|1080p|720p|480p|4k|8k|bluray|blu-ray|bdrip|web-?dl|webrip|hdtv|"
    r"hdr10\+?|hdr|dolby|vision|atmos|dts|aac|ac3|eac3|flac|x264|x265|h\.?264|"
    r"h\.?265|hevc|avc|10bit|8bit|remux|proper|repack|extended|uncut|imax|"
    r"uhd|hd|sd|ma|hdrip|dvdrip|dvdscr|cam|ts|r5|nf|amzn|atvp|dsnp|hmax|"
    r"[\[\(【].*?[\]\)】])",
    re.IGNORECASE,
)


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTS


def is_subtitle(name: str) -> bool:
    return Path(name).suffix.lower() in SUB_EXTS


def _clean_title(t: str) -> str:
    t = _TAG_RE.sub(" ", t)
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -")
    return t


def _regex_parse(stem: str) -> dict:
    season = episode = None
    for pat in _EP_PATTERNS:
        m = pat.search(stem)
        if m:
            if len(m.groups()) == 2:
                season, episode = int(m.group(1)), int(m.group(2))
            else:
                episode = int(m.group(1))
            stem_clean = stem[:m.start()]
            break
    else:
        stem_clean = stem
    year = None
    m = _YEAR_RE.search(stem_clean)
    if m:
        year = int(m.group(1))
        title_part = stem_clean[:m.start()]
    else:
        title_part = stem_clean
    title = _clean_title(title_part) or _clean_title(stem)
    return {"title": title, "year": year, "season": season, "episode": episode}


def parse(filename: str) -> dict:
    """返回 {title, year, season, episode}。episode 非空即视为剧集。"""
    stem = Path(filename).stem
    if _HAS_GUESSIT:
        try:
            g = dict(guessit.guessit(filename))
            title = g.get("title")
            if title:
                season = g.get("season")
                episode = g.get("episode")
                if isinstance(episode, list):
                    episode = episode[0]
                if isinstance(season, list):
                    season = season[0]
                year = g.get("year")
                return {
                    "title": str(title),
                    "year": int(year) if year else None,
                    "season": int(season) if season is not None else None,
                    "episode": int(episode) if episode is not None else None,
                }
        except Exception:
            pass
    return _regex_parse(stem)
