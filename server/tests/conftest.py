"""API test harness.

DB-backed tests need a real Postgres (the schema leans on JSONB, partial
indexes, triggers, and a view — SQLite can't stand in). Set
``VINYL_TEST_DB_URL`` to run them; without it they skip cleanly, so the pure
tests still run anywhere. CI provides a postgres service; locally, point it at
a scratch database.

The Alembic migration is applied once per session — so every API test also
exercises the real DDL, not a parallel create_all() world.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

TEST_DB_URL = os.environ.get("VINYL_TEST_DB_URL")

_SERVER_DIR = Path(__file__).resolve().parents[1]


class FakeDiscogs:
    """Serves canned raw releases; records search calls."""

    def __init__(self, releases: dict[int, dict]):
        self.releases = releases
        self.search_calls = []

    def search(self, query=None, catno=None, artist=None):
        self.search_calls.append({"query": query, "catno": catno, "artist": artist})
        return [
            {"discogs_release_id": rid, "title": r.get("title")}
            for rid, r in self.releases.items()
        ]

    def get_release(self, release_id: int) -> dict:
        return self.releases[release_id]


class FakeS3:
    """In-memory bucket."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key, Range=None):
        data = self.objects[Key]
        if Range:
            spec = Range.split("=", 1)[1]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else len(data) - 1
            data = data[start:end + 1]

        class _Body(io.BytesIO):
            def iter_chunks(self, chunk_size=8192):
                while chunk := self.read(chunk_size):
                    yield chunk

        return {"Body": _Body(data)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


RAW_RELEASE = {
    "id": 249504,
    "title": "Waltz For Debby",
    "year": 1962,
    "country": "US",
    "artists": [{"name": "Bill Evans Trio"}],
    "labels": [{"name": "Riverside Records", "catno": "RLP 9399"}],
    "tracklist": [
        {"type_": "track", "position": "A1", "title": "My Foolish Heart", "duration": "4:56"},
        {"type_": "track", "position": "A2", "title": "Waltz For Debby", "duration": "6:54"},
        {"type_": "track", "position": "B1", "title": "Detour Ahead", "duration": "7:35"},
        {"type_": "track", "position": "B2", "title": "My Romance", "duration": "7:13"},
    ],
}


@pytest.fixture(scope="session")
def engine():
    if TEST_DB_URL is None:
        pytest.skip("VINYL_TEST_DB_URL not set")
    env = dict(os.environ, VINYL_DB_URL=TEST_DB_URL)
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=_SERVER_DIR, env=env, check=True, capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_SERVER_DIR, env=env, check=True, capture_output=True,
    )
    from sqlalchemy import create_engine

    eng = create_engine(TEST_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE station, asset, capture, disc_side, accession, release "
            "RESTART IDENTITY CASCADE"
        ))
        conn.execute(text("UPDATE collection SET next_number = 1"))
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def api(session_factory):
    """TestClient wired to the test DB with fake external clients."""
    from fastapi.testclient import TestClient

    os.environ.setdefault("VINYL_API_KEY", "test-key")
    from vinyl_server.app import create_app
    from vinyl_server.config import settings
    from vinyl_server.db.session import get_session

    settings.cache_clear()
    os.environ["VINYL_API_KEY"] = "test-key"
    # Fill required-but-unused settings so settings() can construct.
    for var, val in {
        "VINYL_DB_URL": TEST_DB_URL or "",
        "VINYL_DISCOGS_TOKEN": "unused",
        "VINYL_S3_ACCESS_KEY": "unused",
        "VINYL_S3_SECRET_KEY": "unused",
    }.items():
        os.environ.setdefault(var, val)

    fake_s3 = FakeS3()
    app = create_app(
        discogs_client=FakeDiscogs({RAW_RELEASE["id"]: RAW_RELEASE}),
        s3_client=fake_s3,
        s3_bucket="vinyl-test",
        musicbrainz_enabled=False,
    )

    def _test_session():
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    spawned: list = []
    app.state.spawn_qc = lambda cid: (spawned.append(cid), True)[1]

    app.dependency_overrides[get_session] = _test_session
    client = TestClient(app)
    client.spawned_qc = spawned
    client.headers["X-API-Key"] = "test-key"
    client.fake_s3 = fake_s3
    return client
