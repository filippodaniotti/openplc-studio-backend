"""Assembly and publication of immutable, run-scoped listening material."""
from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5
from bson import ObjectId

from plctestbench.plc_testbench import PLCTestbench

from plc_platform_backend.modules.modules_models import ModuleType
from plc_platform_backend.runs.runs_models import Run
from .studies_models import ListeningStudyDocument, StudyArtifactSource, StudyStatus
from .studies_repository import get_listening_studies_repository
from .participant_models import MAX_RATINGS_PER_SESSION


def _module_index(modules, node_id: str) -> int:
    matches = [index for index, module in enumerate(modules) if node_id in module.node_ids]
    if len(matches) != 1:
        raise ValueError(f"Execution node {node_id} has no unambiguous module-instance mapping")
    return matches[0]


def _publish_material(study, entries, destination: Path) -> None:
    """Atomically export material, or verify an identical previous preparation."""
    try:
        definition = json.loads(json.dumps(asdict(study.definition)))
    except (ValueError, TypeError) as error:
        raise ValueError("Prepared listening definition is not JSON serializable") from error
    preparation_hashes = sorted(hash(bundle) for bundle, _ in entries)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            existing = json.loads((destination / "definition.json").read_text())
            existing_hashes = json.loads((destination / "preparation-hashes.json").read_text())
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("Published listening definition could not be verified") from error
        if existing != definition or existing_hashes != preparation_hashes:
            raise ValueError("Published listening definition conflicts with prepared material")
        return
    with TemporaryDirectory(prefix=".listening-", dir=destination.parent) as temporary:
        material = Path(temporary) / "material"
        study.export(material)
        (material / "preparation-hashes.json").write_text(json.dumps(preparation_hashes))
        material.rename(destination)


def prepare_studies(run: Run, testbench: PLCTestbench, root_folder: str) -> list[ListeningStudyDocument]:
    """Prepare the run's single study across every selected track and PLC condition.

    Human runs require one HumanCalculator and one loss simulator. Automated-only
    runs have no study. Participant data never enters shared computation artifacts;
    sources are retained as private provenance for run-specific finalization.
    """
    human_modules = run.modules.get(ModuleType.OutputAnalyser, [])
    human_indices = [index for index, module in enumerate(human_modules) if module.name == "HumanCalculator"]
    if not human_indices:
        return []
    simulator_modules = run.modules.get(ModuleType.PacketLossSimulator, [])
    if len(human_indices) != 1 or len(simulator_modules) != 1:
        raise ValueError("Human runs require exactly one HumanCalculator and one loss simulator instance")
    human_index = human_indices[0]
    # Keep automated-only deployments bootable with independently pinned core.
    try:
        from plctestbench.human_listening import HumanCalculatorData, assemble_study
    except ImportError as error:
        raise RuntimeError("Human studies require the updated MUSHRA-enabled plctestbench package") from error
    if not ObjectId.is_valid(run.id):
        raise ValueError("Listening study storage requires a valid platform run ID")
    plc_modules = run.modules[ModuleType.PLCAlgorithm]
    entries: list[tuple[HumanCalculatorData, StudyArtifactSource]] = []
    for root in testbench.data_manager.root_nodes:
        for node in testbench.get_nodes_by_depth(root).get(3, []):
            data = node.get_data()
            if not isinstance(data, HumanCalculatorData):
                continue
            if data.status != "prepared":
                raise ValueError("Shared computation cache must not contain finalized participant responses")
            if not data.excerpts:
                raise ValueError("Prepared human condition has no excerpts")
            if _module_index(human_modules, node.get_id()) != human_index:
                raise ValueError("Prepared human material does not match the configured HumanCalculator")
            _module_index(simulator_modules, node.get_lost_samples_mask_node().get_id())
            plc_index = _module_index(plc_modules, node.get_reconstructed_track_node().get_id())
            condition_id = f"plc-{plc_index}"
            bundle = replace(data, condition_id=condition_id)
            source = StudyArtifactSource(
                node_id=node.get_id(), condition_id=condition_id,
                track_id=bundle.excerpts[0].definition.track_id,
                source_path=node.get_file().get_path(),
            )
            entries.append((bundle, source))
    if not entries:
        raise ValueError("Configured HumanCalculator has no prepared material")
    # Preserve the existing identity format for idempotent publication.
    study_id = str(uuid5(NAMESPACE_URL, f"plc-listening:{run.id}:{human_index}:0:v1"))
    study = assemble_study([bundle for bundle, _ in entries], study_id)
    if sum(len(trial.conditions) for trial in study.definition.trials) > MAX_RATINGS_PER_SESSION:
        raise ValueError(f"Study workload exceeds {MAX_RATINGS_PER_SESSION} ratings per participant; reduce tracks, trials, or repetitions")
    destination = Path(root_folder).resolve() / "listening-studies" / run.id / study_id
    _publish_material(study, entries, destination)
    return [ListeningStudyDocument(
        id=study_id, run_id=run.id,
        human_module_index=human_index, simulator_module_index=0,
        status=StudyStatus.COLLECTING, created=datetime.utcnow(),
        definition=asdict(study.definition),
        conditions=[
            {"id": f"plc-{index}", "module_index": index, "name": module.name}
            for index, module in enumerate(plc_modules)
        ],
        warnings=list(dict.fromkeys(warning for bundle, _ in entries for warning in bundle.warnings)),
        trial_count=len(study.definition.trials),
        artifact_directory=str(destination),
        audio_files=sorted(path.name for path in (destination / "audio").glob("*.wav")),
        sources=[source for _, source in entries],
    )]


async def publish_prepared_studies(studies: list[ListeningStudyDocument]) -> None:
    repository = get_listening_studies_repository()
    for study in studies:
        await repository.save_prepared(study)
