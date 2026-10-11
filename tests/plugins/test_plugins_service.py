import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.modules.modules_models import ModuleType
from plc_platform_backend.modules.modules_repository import ModulesRepository
from plc_platform_backend.plugins.plugins_models import PluginStatus
from plc_platform_backend.plugins.plugins_service import PluginsService, InvalidPluginSource, PluginConflictError


VALID_PLUGIN = '''"""
- name: Demo
  supported_packet_sizes: [128]
  settings:
    - name: strength
      type: float
      default: 0.5
      values: null
"""

class DemoSettings:
    pass

class Demo:
    pass
'''


class PluginsServiceTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.plugins_directory = Path(self.temporary_directory.name)
        self.environment = patch.dict(
            os.environ,
            {
                "MONGO_INITDB_ROOT_USERNAME": "test",
                "MONGO_INITDB_ROOT_PASSWORD": "test",
                "PLC_ROOT_FOLDER": str(self.plugins_directory / "artifacts"),
                "PLUGINS_DIRECTORY": str(self.plugins_directory),
                "REDIS_URL": "redis://localhost:6379/0",
            },
        )
        self.environment.start()
        get_configuration.cache_clear()

    def tearDown(self) -> None:
        get_configuration.cache_clear()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_discovers_available_plugin(self) -> None:
        (self.plugins_directory / "DemoAlgorithm.py").write_text(VALID_PLUGIN)

        inventory = PluginsService().scan()

        self.assertEqual(len(inventory.items), 1)
        plugin = inventory.items[0]
        self.assertEqual(plugin.filename, "DemoAlgorithm.py")
        self.assertEqual(plugin.status, PluginStatus.AVAILABLE)
        self.assertEqual(plugin.module_type, ModuleType.PLCAlgorithm)
        self.assertIsNotNone(plugin.spec)
        assert plugin.spec is not None
        self.assertEqual(plugin.spec.name, "Demo")
        self.assertTrue(plugin.spec.is_plugin)
        self.assertEqual(plugin.spec.supported_packet_sizes, [128])
        self.assertEqual(plugin.spec.settings[0].name, "strength")
        self.assertIsNone(plugin.error)

    def test_upload_saves_valid_plugin_and_rejects_overwrite(self) -> None:
        service = PluginsService()
        plugin = service.upload("DemoAlgorithm.py", VALID_PLUGIN.encode())
        self.assertEqual(plugin.status, PluginStatus.AVAILABLE)
        self.assertEqual(service.scan().items[0].filename, "DemoAlgorithm.py")
        with self.assertRaises(FileExistsError):
            service.upload("DemoAlgorithm.py", VALID_PLUGIN.encode())
        self.assertEqual((self.plugins_directory / "DemoAlgorithm.py").read_text(), VALID_PLUGIN)

    def test_upload_rejects_unsafe_names_and_invalid_content(self) -> None:
        for filename, content in [
            ("../DemoAlgorithm.py", VALID_PLUGIN.encode()),
            ("..\\DemoAlgorithm.py", VALID_PLUGIN.encode()),
            ("Demo.txt", VALID_PLUGIN.encode()),
            ("DemoAlgorithm.py", b""),
            ("DemoAlgorithm.py", b"\xff"),
            ("DemoAlgorithm.py", b"class Demo: pass"),
            ("Wrong.py", VALID_PLUGIN.encode()),
            ("DemoAlgorithm.py", b"x" * (5 * 1024 * 1024 + 1)),
        ]:
            with self.subTest(filename=filename, size=len(content)):
                with self.assertRaises(ValueError):
                    PluginsService().upload(filename, content)
        self.assertEqual(list(self.plugins_directory.iterdir()), [])

    def test_source_roundtrip_preserves_utf8_and_line_endings(self) -> None:
        original = (VALID_PLUGIN + '# café\n').replace('\n', '\r\n')
        path = self.plugins_directory / 'DemoAlgorithm.py'
        path.write_bytes(original.encode())
        path.chmod(0o640)
        service = PluginsService()
        source = service.get_source(path.name)
        self.assertEqual(source.content, original)
        edited = original + '# edited\r\n'
        saved = service.save_source(path.name, edited, source.revision)
        self.assertEqual(path.read_bytes(), edited.encode())
        self.assertEqual(service.get_source(path.name), saved)
        self.assertNotEqual(saved.revision, source.revision)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(saved.plugin.status, PluginStatus.AVAILABLE)

    def test_invalid_plugin_can_be_read_repaired_and_validated_without_execution(self) -> None:
        path = self.plugins_directory / 'DemoAlgorithm.py'
        path.write_text('def broken(:\n')
        service = PluginsService()
        source = service.get_source(path.name)
        self.assertEqual(source.plugin.status, PluginStatus.INVALID)
        result = service.validate_source(path.name, source.content)
        self.assertFalse(result.valid)
        self.assertEqual(result.diagnostics[0].line, 1)
        self.assertIsNotNone(result.diagnostics[0].column)
        repaired = VALID_PLUGIN + '\nraise RuntimeError("must not execute")\n'
        self.assertTrue(service.validate_source(path.name, repaired).valid)
        self.assertEqual(path.read_text(), source.content)
        self.assertEqual(service.save_source(path.name, repaired, source.revision).plugin.status, PluginStatus.AVAILABLE)

    def test_invalid_saves_preserve_original_and_share_upload_validation(self) -> None:
        path = self.plugins_directory / 'DemoAlgorithm.py'
        path.write_text(VALID_PLUGIN)
        service = PluginsService()
        source = service.get_source(path.name)
        for content in ['', 'def broken(:', 'class Demo: pass',
                        VALID_PLUGIN.replace('name: Demo', 'name: Renamed'),
                        VALID_PLUGIN.replace('class DemoSettings:', 'class OtherSettings:'),
                        VALID_PLUGIN.replace('name: Demo', 'settings: []'),
                        'x' * (5 * 1024 * 1024 + 1)]:
            with self.subTest(content=content[:30]):
                result = service.validate_source(path.name, content)
                self.assertFalse(result.valid)
                with self.assertRaises(InvalidPluginSource):
                    service.save_source(path.name, content, source.revision)
                with self.assertRaises(ValueError):
                    service.upload(path.name, content.encode())
                self.assertEqual(path.read_text(), VALID_PLUGIN)

    def test_source_rejects_unsafe_paths_symlinks_missing_and_oversized_files(self) -> None:
        service = PluginsService()
        for filename in ['../DemoAlgorithm.py', '/tmp/Demo.py', '..\\Demo.py', 'Demo.txt', 'bad\x00.py']:
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    service.get_source(filename)
                with self.assertRaises(ValueError):
                    service.save_source(filename, VALID_PLUGIN, 'revision')
        with self.assertRaises(FileNotFoundError):
            service.get_source('Missing.py')
        target = self.plugins_directory / 'target.txt'
        target.write_text(VALID_PLUGIN)
        link = self.plugins_directory / 'DemoAlgorithm.py'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            service.get_source(link.name)
        with self.assertRaises(ValueError):
            service.save_source(link.name, VALID_PLUGIN, 'revision')
        link.unlink()
        link.mkdir()
        with self.assertRaises(ValueError):
            service.get_source(link.name)
        link.rmdir()
        link.write_bytes(b'x' * (5 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            service.get_source(link.name)
        link.write_bytes(b'\xff')
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            service.get_source(link.name)

    def test_stale_revision_preserves_external_changes(self) -> None:
        path = self.plugins_directory / 'DemoAlgorithm.py'
        path.write_text(VALID_PLUGIN)
        service = PluginsService()
        source = service.get_source(path.name)
        external = VALID_PLUGIN + '# external\n'
        path.write_text(external)
        with self.assertRaises(PluginConflictError):
            service.save_source(path.name, VALID_PLUGIN + '# editor\n', source.revision)
        self.assertEqual(path.read_text(), external)

    def test_concurrent_editor_saves_do_not_overwrite_each_other(self) -> None:
        path = self.plugins_directory / 'DemoAlgorithm.py'
        path.write_text(VALID_PLUGIN)
        revision = PluginsService().get_source(path.name).revision

        def save(comment):
            try:
                return PluginsService().save_source(path.name, VALID_PLUGIN + comment, revision)
            except PluginConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, ['# first\n', '# second\n']))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(path.read_text(), winners[0].content)

    def test_storage_failure_leaves_original_and_removes_staged_file(self) -> None:
        path = self.plugins_directory / 'DemoAlgorithm.py'
        path.write_text(VALID_PLUGIN)
        service = PluginsService()
        revision = service.get_source(path.name).revision
        with patch('plc_platform_backend.plugins.plugins_service.os.replace', side_effect=PermissionError(13, 'Permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'Could not save plugin'):
                service.save_source(path.name, VALID_PLUGIN + '# change\n', revision)
        self.assertEqual(path.read_text(), VALID_PLUGIN)
        self.assertEqual(list(self.plugins_directory.glob('*.tmp')), [])

    def test_reports_invalid_plugins(self) -> None:
        cases = [
            ("BrokenAlgorithm.py", "def broken(:\n", "Python syntax error"),
            ("BrokenAlgorithm.py", "class Broken: pass\n", "Missing module docstring"),
            ("BrokenAlgorithm.py", '"""not: [valid"""', "while parsing"),
            (
                "BrokenAlgorithm.py",
                '"""\n- settings: []\n"""\nclass BrokenSettings: pass\nclass Broken: pass\n',
                "Invalid manifest",
            ),
            ("WrongAlgorithm.py", VALID_PLUGIN, "Expected filename 'DemoAlgorithm.py'"),
            (
                "DemoAlgorithm.py",
                '"""\n- name: Demo\n  settings: []\n"""\nclass Demo: pass\n',
                "Missing required class(es): DemoSettings",
            ),
        ]

        for filename, content, message in cases:
            with self.subTest(filename=filename, message=message):
                for path in self.plugins_directory.glob("*.py"):
                    path.unlink()
                (self.plugins_directory / filename).write_text(content)

                plugin = PluginsService().scan().items[0]

                self.assertEqual(plugin.status, PluginStatus.INVALID)
                self.assertIsNone(plugin.spec)
                self.assertIn(message, plugin.error or "")

    def test_scan_is_sorted_and_reflects_filesystem_changes(self) -> None:
        (self.plugins_directory / "ZuluAlgorithm.py").write_text(
            VALID_PLUGIN.replace("Demo", "Zulu")
        )
        (self.plugins_directory / "AlphaAlgorithm.py").write_text(
            VALID_PLUGIN.replace("Demo", "Alpha")
        )
        service = PluginsService()

        self.assertEqual(
            [item.filename for item in service.scan().items],
            ["AlphaAlgorithm.py", "ZuluAlgorithm.py"],
        )

        (self.plugins_directory / "AlphaAlgorithm.py").unlink()
        self.assertEqual(
            [item.filename for item in service.scan().items],
            ["ZuluAlgorithm.py"],
        )

    def test_file_read_errors_do_not_expose_absolute_paths(self) -> None:
        plugin_path = self.plugins_directory / "DemoAlgorithm.py"
        plugin_path.write_text(VALID_PLUGIN)

        with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied", str(plugin_path))):
            plugin = PluginsService().scan().items[0]

        self.assertEqual(plugin.status, PluginStatus.INVALID)
        self.assertEqual(plugin.error, "Could not read plugin file: Permission denied")
        self.assertNotIn(str(self.plugins_directory), plugin.error or "")

    def test_invalid_plugin_does_not_hide_available_plugins(self) -> None:
        (self.plugins_directory / "DemoAlgorithm.py").write_text(VALID_PLUGIN)
        (self.plugins_directory / "BrokenAlgorithm.py").write_text("def broken(:")

        inventory = PluginsService().scan()

        self.assertEqual(
            {item.status for item in inventory.items},
            {PluginStatus.AVAILABLE, PluginStatus.INVALID},
        )

    def test_modules_repository_only_returns_available_plugins(self) -> None:
        (self.plugins_directory / "DemoAlgorithm.py").write_text(VALID_PLUGIN)
        (self.plugins_directory / "BrokenAlgorithm.py").write_text("def broken(:")

        modules = ModulesRepository().get_all_modules_by_type(ModuleType.PLCAlgorithm)

        self.assertIn("Demo", [module.name for module in modules])
        self.assertNotIn("Broken", [module.name for module in modules])
        demo = next(module for module in modules if module.name == "Demo")
        self.assertTrue(demo.is_plugin)
        self.assertEqual(demo.supported_packet_sizes, [128])
        self.assertFalse(next(module for module in modules if module.name == "AdvancedPLC").is_plugin)
