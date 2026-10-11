from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plc_platform_backend.plugins.plugins_controller import router
from plc_platform_backend.plugins.plugins_models import (
    PluginInventory,
    PluginInventoryItem,
    PluginStatus,
    PluginSource,
    PluginValidation,
    PluginDiagnostic,
)
from plc_platform_backend.plugins.plugins_service import get_plugins_service, InvalidPluginSource, PluginConflictError


class PluginsControllerTests(TestCase):
    def setUp(self) -> None:
        self.service = Mock()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_plugins_service] = lambda: self.service
        self.client = TestClient(app)

    def test_source_endpoints_return_source_validation_and_save_results(self) -> None:
        source = PluginSource(
            filename='DemoAlgorithm.py', content='source', revision='hash',
            plugin=PluginInventoryItem(filename='DemoAlgorithm.py', status=PluginStatus.AVAILABLE),
        )
        self.service.get_source.return_value = source
        response = self.client.get('/plugins/DemoAlgorithm.py/source')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['content'], 'source')
        self.assertNotIn('path', response.json())
        self.service.get_source.assert_called_once_with('DemoAlgorithm.py')

        self.service.validate_source.return_value = PluginValidation(valid=False, diagnostics=[
            PluginDiagnostic(message='Syntax error', line=1, column=5),
        ])
        response = self.client.post('/plugins/DemoAlgorithm.py/validate', json={'content': 'draft'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['valid'])
        self.assertEqual(response.json()['diagnostics'][0]['line'], 1)
        self.service.validate_source.assert_called_once_with('DemoAlgorithm.py', 'draft')

        self.service.save_source.return_value = source
        response = self.client.put('/plugins/DemoAlgorithm.py/source', json={'content': 'draft', 'revision': 'hash'})
        self.assertEqual(response.status_code, 200)
        self.service.save_source.assert_called_once_with('DemoAlgorithm.py', 'draft', 'hash')

    def test_editor_endpoints_map_storage_conflict_and_validation_errors(self) -> None:
        for error, status in [(FileNotFoundError('Not found'), 404), (ValueError('Unsafe path'), 400),
                              (RuntimeError('Storage error'), 500), (PluginConflictError('Changed'), 409)]:
            with self.subTest(status=status):
                self.service.save_source.side_effect = error
                response = self.client.put('/plugins/DemoAlgorithm.py/source', json={'content': 'draft', 'revision': 'hash'})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()['detail'], str(error))
        self.service.save_source.side_effect = InvalidPluginSource([PluginDiagnostic(message='Bad syntax', line=2, column=4)])
        response = self.client.put('/plugins/DemoAlgorithm.py/source', json={'content': 'draft', 'revision': 'hash'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail']['diagnostics'][0]['line'], 2)

    def test_save_requires_revision_and_source_content(self) -> None:
        response = self.client.put('/plugins/DemoAlgorithm.py/source', json={'content': 'draft'})
        self.assertEqual(response.status_code, 422)
        self.service.save_source.assert_not_called()

    def test_returns_inventory_without_server_paths(self) -> None:
        self.service.scan.return_value = PluginInventory(
            scanned_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            items=[
                PluginInventoryItem(
                    filename="BrokenAlgorithm.py",
                    status=PluginStatus.INVALID,
                    error="Missing manifest",
                )
            ],
        )

        response = self.client.get("/plugins")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["items"][0]["filename"], "BrokenAlgorithm.py")
        self.assertNotIn("directory", payload)
        self.assertNotIn("path", payload["items"][0])

    def test_maps_directory_errors_to_500(self) -> None:
        self.service.scan.side_effect = RuntimeError("Could not read plugins")

        response = self.client.get("/plugins")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Could not read plugins")

    def test_upload_passes_file_to_service(self) -> None:
        self.service.upload.return_value = PluginInventoryItem(
            filename="DemoAlgorithm.py", status=PluginStatus.AVAILABLE,
        )
        response = self.client.post("/plugins", files={"file": ("DemoAlgorithm.py", b"plugin")})
        self.assertEqual(response.status_code, 201)
        self.service.upload.assert_called_once_with("DemoAlgorithm.py", b"plugin")

    def test_upload_maps_errors(self) -> None:
        for error, status in [(ValueError("Invalid plugin"), 400),
                              (FileExistsError("Already exists"), 409),
                              (RuntimeError("Storage error"), 500)]:
            with self.subTest(status=status):
                self.service.upload.side_effect = error
                response = self.client.post("/plugins", files={"file": ("DemoAlgorithm.py", b"plugin")})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"], str(error))
