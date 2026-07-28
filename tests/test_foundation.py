"""平台底座的轻量接口回归。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_runtime = tempfile.TemporaryDirectory()
_root = Path(_runtime.name)
(_root / "config").mkdir()
(_root / "media" / "movies").mkdir(parents=True)
(_root / "media" / "tv").mkdir()
os.environ["CONFIG_DIR"] = str(_root / "config")
os.environ["MEDIA_ROOT"] = str(_root / "media")
os.environ["MEDIAHUB_DISABLE_WORKERS"] = "1"

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.downloader import add_tasks
from app.providers import get_provider, provider_names


def _media() -> int:
    path = _root / "media" / "movies" / "Foundation.2026.mp4"
    path.touch()
    return db.exe(
        """INSERT INTO media(type,path,title,year,size,mtime,status,created_at)
           VALUES('movie',?,'Foundation',2026,123,1,'ok',?)""",
        (str(path), db.now()),
    )


def _show() -> None:
    folder = _root / "media" / "tv" / "Foundation"
    folder.mkdir()
    for episode, title in ((1, "Foundation"), (2, "Unrelated Episode")):
        path = folder / f"Foundation.S01E{episode:02d}.mkv"
        path.touch()
        db.exe(
            """INSERT INTO media(type,path,title,year,season,episode,tmdb_id,
                                 size,mtime,status,created_at)
               VALUES('episode',?,?,2026,1,?,777,123,1,'ok',?)""",
            (str(path), title, episode, db.now()),
        )


def test_provider_registry_and_schema_migration():
    assert provider_names() == ["115"]
    assert get_provider("115").name == "115"
    task_columns = {r["name"] for r in db.q("PRAGMA table_info(tasks)")}
    assert {"provider", "account_id"} <= task_columns
    item = {"pickcode": "same-remote-id", "name": "episode.mkv", "id": "1"}
    assert add_tasks([item], str(_root / "media"), account_id="account-a") == (1, 0)
    assert add_tasks([item], str(_root / "media"), account_id="account-b") == (1, 0)


def test_device_progress_and_filtered_library():
    with TestClient(app) as client:
        media_id = _media()
        _show()
        response = client.post(
            "/api/devices/register",
            json={
                "id": "tv-test-device",
                "name": "Old Xiaomi",
                "platform": "android_tv",
                "capabilities": {"androidApi": 23, "height": 720},
            },
        )
        assert response.status_code == 200

        response = client.post(
            f"/api/progress/{media_id}",
            json={
                "position_ms": 180_000,
                "duration_ms": 5_400_000,
                "device_id": "tv-test-device",
            },
        )
        assert response.status_code == 200
        assert client.get(f"/api/progress/{media_id}").json()["position_ms"] == 180_000
        assert client.get("/api/progress").json()["items"][0]["media_id"] == media_id

        library = client.get(
            "/api/library?q=foundation&year=2026&type=movie"
        ).json()
        assert library["total"] == 1
        shows = client.get(
            "/api/library?q=foundation&year=2026&type=show"
        ).json()
        assert shows["total"] == 1
        assert shows["items"][0]["count"] == 2
