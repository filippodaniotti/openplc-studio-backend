from plc_platform_backend.modules.modules_models import (
    Module,
    ModuleParameter,
    ModuleType,
)
from plc_platform_backend.modules.modules_repository import ModulesRepository
from plc_platform_backend.modules.modules_service import ModuleService
from plc_platform_backend.modules.modules_validator import ModuleConfigValidator


def test_spectral_energy_calculator_is_exposed_with_testbench_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGO_INITDB_ROOT_USERNAME", "test")
    monkeypatch.setenv("MONGO_INITDB_ROOT_PASSWORD", "test")
    monkeypatch.setenv("PLC_ROOT_FOLDER", str(tmp_path))
    monkeypatch.setenv("PLUGINS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    modules = ModulesRepository().get_all_modules_by_type(ModuleType.OutputAnalyser)
    spectral = next(module for module in modules if module.name == "SpectralEnergyCalculator")

    assert [(setting.name, setting.type, setting.default) for setting in spectral.settings] == [
        ("N", "int", 1024),
        ("hop", "int", None),
        ("amp_scale", "float", 1),
    ]

    validator = ModuleConfigValidator(ModuleService())
    messages = validator.validate_module(
        ModuleType.OutputAnalyser,
        Module(
            name="SpectralEnergyCalculator",
            settings=[
                ModuleParameter(name="N", value=1024),
                ModuleParameter(name="hop", value=None),
                ModuleParameter(name="amp_scale", value=1),
            ],
        ),
    )

    assert messages == []
