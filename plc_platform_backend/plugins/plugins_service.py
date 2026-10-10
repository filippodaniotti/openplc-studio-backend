from __future__ import annotations

import ast
import os
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
)


@lru_cache
def get_plugins_service() -> PluginsService:
    return PluginsService()


class PluginsService:
    def upload(self, filename: str, content: bytes) -> PluginInventoryItem:
        if not filename or "/" in filename or "\\" in filename or not filename.endswith(".py"):
            raise ValueError("Choose a Python plugin file (.py) with a plain filename")
        if not content:
            raise ValueError("The plugin file is empty")
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("Plugin files must be 5 MB or smaller")
        try:
            tree = ast.parse(content.decode("utf-8"), filename=filename)
            spec = self._parse_manifest(tree)
            self._validate_runtime_shape(filename, tree, spec)
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
            content = plugin_path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=plugin_path.name)
            spec = self._parse_manifest(tree)
            self._validate_runtime_shape(plugin_path.name, tree, spec)
            return PluginInventoryItem(
                filename=plugin_path.name,
                status=PluginStatus.AVAILABLE,
                spec=spec,
            )
        except (OSError, SyntaxError, ValueError, yaml.YAMLError, ValidationError) as error:
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
