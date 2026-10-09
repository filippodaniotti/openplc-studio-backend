from plctestbench.settings import HumanCalculatorSettings

from plc_platform_backend.modules.modules_models import Module, ModuleParameter, ModuleType
from plc_platform_backend.modules.modules_repository import ModulesRepository
from plc_platform_backend.modules.modules_service import ModuleService
from plc_platform_backend.modules.modules_validator import ModuleConfigValidator


def test_human_manifest_defaults_match_the_core_constructor(monkeypatch, tmp_path):
    for key, value in {
        "MONGO_INITDB_ROOT_USERNAME": "test", "MONGO_INITDB_ROOT_PASSWORD": "test",
        "PLC_ROOT_FOLDER": str(tmp_path), "PLUGINS_DIRECTORY": str(tmp_path),
        "REDIS_URL": "redis://localhost:6379/0",
    }.items():
        monkeypatch.setenv(key, value)
    spec = next(module for module in ModulesRepository().get_all_modules_by_type(ModuleType.OutputAnalyser) if module.name == "HumanCalculator")
    defaults = {parameter.name: parameter.default for parameter in spec.settings}
    assert HumanCalculatorSettings(**defaults).get_all() == defaults
    assert {parameter.type for parameter in spec.settings} <= {"int", "float", "bool", "Enum"}
    module = Module(name=spec.name, settings=[ModuleParameter(name=name, value=value) for name, value in defaults.items()])
    validator = ModuleConfigValidator(ModuleService())
    assert validator.validate_module(ModuleType.OutputAnalyser, module) == []
    for name, value in (("test_type", "mos"), ("stimulus_length", 999), ("trials_per_track", 0), ("iterations", 0), ("choose_seed", -1), ("loudness_threshold_db", 1)):
        invalid = Module(name=spec.name, settings=[ModuleParameter(name=key, value=value if key == name else default) for key, default in defaults.items()])
        errors = validator.validate_module(ModuleType.OutputAnalyser, invalid)
        assert any(error.setting == name for error in errors)
