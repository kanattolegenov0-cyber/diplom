import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.anime import AnimeError, AnimeService, GACHIAKUTA, player_url
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


async def test_anime_returns_only_media_not_site_or_ads():
    html = '''<iframe src="https://ads.example/banner"></iframe>
        <iframe src="https://animego.work/370-gachiakuta-2025.html"></iframe>
        <iframe data-src="//kodik.info/serial/123/abc/720p?episode=3"></iframe>
        <video controls><source src="https://media.example/episode.mp4?token=123"></video>'''
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))) as client:
        result = await AnimeService(client).player(GACHIAKUTA)
    assert result['players'] == [
        {'kind': 'embed', 'url': 'https://kodik.info/serial/123/abc/720p?episode=3'},
        {'kind': 'video', 'url': 'https://media.example/episode.mp4?token=123'},
    ]


async def test_anime_without_player_does_not_fall_back_to_full_site():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<h1>Series page</h1>'))) as client:
        with pytest.raises(AnimeError, match='бейнеплеер'):
            await AnimeService(client).player(GACHIAKUTA)


@pytest.mark.parametrize('url,kind', [
    ('http://kodik.info/serial/123/abc/720p', 'embed'),
    ('https://kodik.info.evil.test/serial/123/abc/720p', 'embed'),
    ('https://127.0.0.1/episode.mp4', 'video'),
    ('javascript:alert(1)', 'embed'),
    ('https://animego.work/370-gachiakuta-2025.html', 'embed'),
])
def test_player_rejects_non_media_or_private_urls(url, kind):
    assert player_url(url, kind, GACHIAKUTA) is None
