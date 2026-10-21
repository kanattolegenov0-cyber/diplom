import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.music import MusicService
from app.storage import Store


async def test_soundcloud_metadata_search_caches_and_never_downloads(monkeypatch):
    calls = []

    class Extractor:
        def __init__(self, options):
            assert options['extract_flat'] and options['skip_download']
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, query, download):
            assert download is False
            calls.append(query)
            return {'entries': [{'id': '12345', 'title': 'Song', 'uploader': 'Artist'}, {'id': '../../bad'}]}

    monkeypatch.setattr('app.music.YoutubeDL', Extractor)
    service = MusicService()
    result = await service.soundcloud('Song')
    assert result['tracks'] == [{'id': 'sc:12345', 'source': 'soundcloud', 'title': 'Song', 'channel': 'Artist'}]
    assert await service.soundcloud('song') == result
    assert calls == ['scsearch5:Song']


def test_soundcloud_api_and_playlist(tmp_path):
    class Music:
        async def soundcloud(self, query):
            return {'tracks': [{'id': 'sc:12345', 'title': query}], 'source': 'soundcloud'}
    with TestClient(create_app(tmp_path, music=Music())) as client:
        assert client.get('/api/music/search', params={'q': 'Song', 'source': 'soundcloud'}).json()['source'] == 'soundcloud'
        assert client.get('/api/music/search', params={'q': 'Song', 'source': 'unknown'}).status_code == 422
        payload = {'video_id': 'sc:12345', 'title': 'Song', 'channel': 'Artist', 'session_id': 'listen-sc-123', 'listened_seconds': 35}
        assert client.post('/api/music/listens', json=payload).status_code == 200
        assert client.post('/api/music/listens', json=payload).status_code == 200
        track = client.get('/api/music/playlist').json()['tracks'][0]
        assert track['id'] == 'sc:12345' and track['play_count'] == 1
        assert track['thumbnail'] == ''


def test_upgrade_preserves_existing_youtube_listens(tmp_path):
    with sqlite3.connect(tmp_path / 'ayan.sqlite3') as connection:
        connection.executescript('''
            CREATE TABLE tracks (
                video_id TEXT PRIMARY KEY NOT NULL CHECK(length(video_id) = 11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
                title TEXT NOT NULL, channel TEXT NOT NULL, play_count INTEGER NOT NULL,
                total_listen_seconds REAL NOT NULL, last_played_at TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE play_sessions (
                session_id TEXT PRIMARY KEY NOT NULL,
                video_id TEXT NOT NULL CHECK(length(video_id) = 11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'),
                listened_seconds REAL NOT NULL, counted INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO tracks VALUES ('AAAAAAAAAAA', 'Old song', 'Artist', 1, 35, '2026-01-01', '2026-01-01');
            INSERT INTO play_sessions VALUES ('old-session', 'AAAAAAAAAAA', 35, 1, '2026-01-01', '2026-01-01');
        ''')
    store = Store(tmp_path)
    store.record_listen('AAAAAAAAAAA', 'Old song', 'Artist', 'old-session', 35)
    store.record_listen('sc:12345', 'New song', 'Artist', 'new-session', 35)
    reopened = Store(tmp_path)
    tracks = {track['id']: track for track in reopened.frequent_tracks()}
    assert set(tracks) == {'AAAAAAAAAAA', 'sc:12345'}
    assert all(track['play_count'] == 1 for track in tracks.values())
    assert reopened.entries() == []
