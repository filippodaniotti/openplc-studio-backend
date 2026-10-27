import json
from pathlib import Path
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from plc_platform_backend.listening_studies import client_assets


def test_static_client_and_free_license_source_are_packaged():
    app = FastAPI()
    client_assets.mount_listening_client(app)
    client = TestClient(app)
    index = client.get('/listening-client/index.html')
    assert index.status_code == 200
    assert 'Third-Party Modified Version of the webMUSHRA Software' in index.text
    assert client.get('/listening-client/LICENSE.txt').status_code == 200
    assert client.get('/listening-client/THIRD-PARTY-NOTICES.txt').status_code == 200
    assert client.get('/listening-client/source.zip').status_code == 200
    assert client.get('/listening-client/../participant_service.py').status_code == 404


def test_player_assets_revalidate_and_versioned_urls_work():
    app = FastAPI()
    client_assets.mount_listening_client(app)
    client = TestClient(app)
    revision = 'shared-faders-20261016-2'
    index = client.get(f'/listening-client/index.html?v={revision}')
    assert f'player.css?v={revision}' in index.text
    assert f'player.js?v={revision}' in index.text
    for asset in ('index.html', 'player.css', 'player.js'):
        url = f'/listening-client/{asset}?v={revision}'
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-cache'
        cached = client.get(url, headers={'If-None-Match': response.headers['etag']})
        assert cached.status_code == 304
        assert cached.headers['cache-control'] == 'no-cache'


def test_source_archive_matches_the_deployed_modifications_and_pin():
    assets = Path(client_assets.__file__).resolve().parent.parent / 'static' / 'listening-client'
    manifest = json.loads((assets / 'upstream.json').read_text())
    assert manifest['upstream_commit'] == '8c353f738aa88fa9dee0dc1aa7d18d2a06441cb5'
    with ZipFile(assets / 'source.zip') as archive:
        assert 'upstream-original/LICENSE.txt' in archive.namelist()
        for path in assets.rglob('*'):
            if path.is_file() and path.name != 'source.zip':
                assert archive.read('modified/' + path.relative_to(assets).as_posix()) == path.read_bytes()
