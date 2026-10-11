from __future__ import annotations

import ast
import fcntl
import hashlib
import os
import stat
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.modules.modules_models import ModuleSpec
from plc_platform_backend.plugins.plugins_models import (
    PluginInventory,
    PluginInventoryItem,
    PluginStatus,
    PluginDiagnostic,
    PluginSource,
    PluginValidation,
)

MAX_PLUGIN_BYTES = 5 * 1024 * 1024


class PluginConflictError(Exception):
    pass


class InvalidPluginSource(ValueError):
    def __init__(self, diagnostics: list[PluginDiagnostic]):
        self.diagnostics = diagnostics
        super().__init__(diagnostics[0].message)


@lru_cache
def get_plugins_service() -> PluginsService:
    return PluginsService()


class PluginsService:
    @staticmethod
    def _check_filename(filename: str) -> None:
        if (not filename or "/" in filename or "\\" in filename
                or any(ord(char) < 32 for char in filename) or not filename.endswith(".py")):
            raise ValueError("Choose a Python plugin file (.py) with a plain filename")

    def _plugin_path(self, filename: str) -> Path:
        self._check_filename(filename)
        return Path(get_configuration().plugins_directory) / filename

    def _read_plugin(self, filename: str) -> tuple[bytes, int]:
        path = self._plugin_path(filename)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Plugin must be a regular file, not a directory or symbolic link")
                with os.fdopen(descriptor, "rb", closefd=False) as source:
                    content = source.read(MAX_PLUGIN_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(content) > MAX_PLUGIN_BYTES:
                raise ValueError("Plugin files must be 5 MB or smaller")
            return content, stat.S_IMODE(info.st_mode)
        except FileNotFoundError:
            raise FileNotFoundError("Plugin not found") from None
        except OSError as error:
            if path.is_symlink():
                raise ValueError("Symbolic links cannot be edited") from None
            raise RuntimeError(f"Could not read plugin: {error.strerror or 'Storage error'}") from error

    def _validate_content(self, filename: str, content: bytes) -> ModuleSpec:
        self._check_filename(filename)
        if not content:
            raise ValueError("The plugin file is empty")
        if len(content) > MAX_PLUGIN_BYTES:
            raise ValueError("Plugin files must be 5 MB or smaller")
        tree = ast.parse(content.decode("utf-8"), filename=filename)
        spec = self._parse_manifest(tree)
        self._validate_runtime_shape(filename, tree, spec)
        return spec

    def _inspect_content(self, filename: str, content: bytes) -> tuple[PluginInventoryItem, list[PluginDiagnostic]]:
        try:
            spec = self._validate_content(filename, content)
            return PluginInventoryItem(filename=filename, status=PluginStatus.AVAILABLE, spec=spec), []
        except (UnicodeError, SyntaxError, ValueError, yaml.YAMLError, ValidationError) as error:
            message = self._format_error(error)
            diagnostic = PluginDiagnostic(
                message=message,
                line=error.lineno if isinstance(error, SyntaxError) else None,
                column=error.offset if isinstance(error, SyntaxError) else None,
            )
            return PluginInventoryItem(filename=filename, status=PluginStatus.INVALID, error=message), [diagnostic]

    def _source(self, filename: str, content: bytes) -> PluginSource:
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            raise ValueError("Plugin source must be UTF-8 text") from None
        item, _ = self._inspect_content(filename, content)
        return PluginSource(filename=filename, content=text, revision=hashlib.sha256(content).hexdigest(), plugin=item)

    def get_source(self, filename: str) -> PluginSource:
        content, _ = self._read_plugin(filename)
        return self._source(filename, content)

    def validate_source(self, filename: str, content: str) -> PluginValidation:
        self._read_plugin(filename)
        _, diagnostics = self._inspect_content(filename, self._encode_source(content))
        return PluginValidation(valid=not diagnostics, diagnostics=diagnostics)

    @staticmethod
    def _encode_source(content: str) -> bytes:
        try:
            return content.encode("utf-8")
        except UnicodeError:
            raise ValueError("Plugin source must be UTF-8 text") from None

    def save_source(self, filename: str, content: str, revision: str) -> PluginSource:
        path = self._plugin_path(filename)
        encoded = self._encode_source(content)
        _, diagnostics = self._inspect_content(filename, encoded)
        if diagnostics:
            raise InvalidPluginSource(diagnostics)
        staged_path: Path | None = None
        try:
            # A filesystem lock serializes editor saves across API worker processes.
            lock_path = path.parent / ".plugin-editor.lock"
            self._read_plugin(filename)
            with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                original, mode = self._read_plugin(filename)
                if hashlib.sha256(original).hexdigest() != revision:
                    raise PluginConflictError("This plugin changed since you opened it. Reload before saving.")
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as destination:
                    staged_path = Path(destination.name)
                    destination.write(encoded)
                    destination.flush()
                    os.fchmod(destination.fileno(), mode)
                    os.fsync(destination.fileno())
                # Recheck manual filesystem changes made while preparing the replacement.
                latest, _ = self._read_plugin(filename)
                if hashlib.sha256(latest).hexdigest() != revision:
                    raise PluginConflictError("This plugin changed since you opened it. Reload before saving.")
                os.replace(staged_path, path)
        except FileNotFoundError:
            raise FileNotFoundError("Plugin not found") from None
        except OSError as error:
            raise RuntimeError(f"Could not save plugin: {error.strerror or 'Storage error'}") from error
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
        return self._source(filename, encoded)

    def upload(self, filename: str, content: bytes) -> PluginInventoryItem:
        self._check_filename(filename)
        try:
            spec = self._validate_content(filename, content)
        except (UnicodeError, SyntaxError, ValueError, yaml.YAMLError, ValidationError) as error:
            raise ValueError(self._format_error(error)) from error

        directory = Path(get_configuration().plugins_directory)
        staged_path: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as destination:
                staged_path = Path(destination.name)
                destination.write(content)
            os.link(staged_path, directory / filename)
        except FileExistsError:
            raise FileExistsError(f"Plugin '{filename}' already exists") from None
        except OSError as error:
            raise RuntimeError(f"Could not save plugin: {error.strerror or 'Storage error'}") from error
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
        return PluginInventoryItem(filename=filename, status=PluginStatus.AVAILABLE, spec=spec)

    def scan(self) -> PluginInventory:
        plugins_path = Path(get_configuration().plugins_directory)
        if not plugins_path.exists():
            return PluginInventory(scanned_at=datetime.now(timezone.utc), items=[])

        try:
            plugin_files = sorted(
                path for path in plugins_path.iterdir() if path.is_file() and path.suffix == ".py"
            )
        except OSError as error:
            raise RuntimeError(
                f"Could not read the plugins directory: {error.strerror or str(error)}"
            ) from error

        return PluginInventory(
            scanned_at=datetime.now(timezone.utc),
            items=[self._inspect_plugin(path) for path in plugin_files],
        )

    def get_available_specs(self) -> list[ModuleSpec]:
        try:
            inventory = self.scan()
        except RuntimeError:
            return []
        return [
            item.spec
            for item in inventory.items
            if item.status == PluginStatus.AVAILABLE and item.spec is not None
        ]

    def _inspect_plugin(self, plugin_path: Path) -> PluginInventoryItem:
        try:
            content = plugin_path.read_text(encoding="utf-8").encode("utf-8")
            return self._inspect_content(plugin_path.name, content)[0]
        except (OSError, UnicodeError) as error:
            return PluginInventoryItem(
                filename=plugin_path.name,
                status=PluginStatus.INVALID,
                error=self._format_error(error),
            )

    @staticmethod
    def _parse_manifest(tree: ast.Module) -> ModuleSpec:
        docstring = ast.get_docstring(tree)
        if not docstring:
            raise ValueError("Missing module docstring manifest")

        manifest: Any = yaml.safe_load(docstring)
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise ValueError("Plugin manifest must contain exactly one module entry")
        if not isinstance(manifest[0], dict):
            raise ValueError("Plugin manifest entry must be an object")

        return ModuleSpec.model_validate({**manifest[0], "is_plugin": True})

    @staticmethod
    def _validate_runtime_shape(filename: str, tree: ast.Module, spec: ModuleSpec) -> None:
        expected_filename = f"{spec.name}Algorithm.py"
        if filename != expected_filename:
            raise ValueError(f"Expected filename '{expected_filename}' for plugin '{spec.name}'")

        class_names = {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        }
        required_classes = {spec.name, f"{spec.name}Settings"}
        missing = sorted(required_classes - class_names)
        if missing:
            raise ValueError(f"Missing required class(es): {', '.join(missing)}")

    @staticmethod
    def _format_error(error: Exception) -> str:
        if isinstance(error, OSError):
            return f"Could not read plugin file: {error.strerror or error.__class__.__name__}"
        if isinstance(error, SyntaxError):
            location = f" on line {error.lineno}" if error.lineno else ""
            return f"Python syntax error{location}: {error.msg}"
        if isinstance(error, ValidationError):
            first = error.errors()[0]
            location = ".".join(str(part) for part in first["loc"])
            return f"Invalid manifest at {location}: {first['msg']}"
        return str(error)
