"""Frozen human-result publication; never rerun PLC or mutate shared caches."""
import asyncio
import csv
from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .results_service import AnalysisSnapshotsRepository, response_rows, session_metadata, study_results
from .participant_repository import get_participant_repository
from .studies_models import ListeningStudyDocument, StudyStatus
from .studies_repository import get_listening_studies_repository


RAW_COLUMNS = ["session_id", "session_status", "trial_id", "track_id", "excerpt_id", "repetition", "condition_id", "condition_role", "score", "answered", "included", "excluded", "exclusion_reason", "consent_version", "consent_acknowledged", "audio_context_sample_rate"]


def safe_csv_row(row):
    return {
        key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
        for key, value in row.items() if key in RAW_COLUMNS
    }


def publish_final_artifacts(study: ListeningStudyDocument, result: dict, rows: list[dict], sessions: list[dict]) -> tuple[str, dict[str, str]]:
    from plctestbench.file_wrapper import DataFile
    from plctestbench.human_listening import HumanCalculatorData, assemble_study

    if study.status != StudyStatus.CLOSED:
        raise ValueError("Final publication requires a closed, frozen study")
    destination = Path(study.artifact_directory) / "finalized"
    if destination.exists():
        try:
            manifest = json.loads((destination / "manifest.json").read_text())
        except (OSError, ValueError) as error:
            raise RuntimeError("Final listening artifact manifest is unreadable") from error
        if manifest["study_id"] != study.id or manifest["frozen_revision"] != study.revision or manifest["analysis_input_digest"] != result["analysis_input_digest"]:
            raise RuntimeError("Final listening artifacts conflict with the frozen study")
        outputs = {node: str(destination / relative) for node, relative in manifest["outputs"].items()}
        required = [destination / name for name in ("results.json", "responses.json", "responses.csv", "definition.json", "sessions.json")]
        if not all(Path(path).is_file() for path in outputs.values()) or not all(path.is_file() for path in required):
            raise RuntimeError("Published human output artifacts are missing")
        return str(destination), outputs
    bundles = []
    for source in study.sources:
        data = DataFile(path=source.source_path).get_data()
        if not isinstance(data, HumanCalculatorData) or data.status != "prepared":
            raise RuntimeError("Expected a shared prepared human condition artifact")
        bundles.append(replace(data, condition_id=source.condition_id))
    try:
        expected_hashes = json.loads((Path(study.artifact_directory) / "preparation-hashes.json").read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError("Prepared listening material fingerprint is unreadable") from error
    if sorted(hash(bundle) for bundle in bundles) != expected_hashes:
        raise RuntimeError("Shared preparation changed after the listening study was published")
    assembled = assemble_study(bundles, study.id)
    try:
        canonical = json.loads(json.dumps(asdict(assembled.definition)))
        expected_definition = json.loads(json.dumps(study.definition))
    except (ValueError, TypeError) as error:
        raise RuntimeError("Prepared listening definition is invalid") from error
    if canonical != expected_definition:
        raise RuntimeError("Prepared listening definition changed after publication")
    included = [
        {key: row[key] for key in ("session_id", "trial_id", "condition_id", "score", "answered")}
        for row in rows if row["included"]
    ]
    finalized = assembled.finalize(included)
    with TemporaryDirectory(prefix=".finalize-", dir=study.artifact_directory) as temporary:
        directory = Path(temporary) / "material"
        (directory / "outputs").mkdir(parents=True)
        outputs = {}
        for index, (source, bundle) in enumerate(zip(study.sources, finalized)):
            relative = f"outputs/{index}.json"
            payload = bundle.to_dict()
            # Export the complete raw study dataset only once, not once per node.
            payload.pop("responses")
            payload["source_node_id"] = source.node_id
            (directory / relative).write_text(json.dumps(payload, allow_nan=False))
            outputs[source.node_id] = relative
        result = {**result, "status": "finalized", "provisional": False}
        (directory / "results.json").write_text(json.dumps(result, allow_nan=False))
        (directory / "responses.json").write_text(json.dumps(rows, allow_nan=False))
        (directory / "sessions.json").write_text(json.dumps(sessions, allow_nan=False))
        (directory / "definition.json").write_text(json.dumps(study.definition, allow_nan=False))
        with (directory / "responses.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=RAW_COLUMNS)
            writer.writeheader()
            writer.writerows(safe_csv_row(row) for row in rows)
        (directory / "manifest.json").write_text(json.dumps({
            "study_id": study.id, "frozen_revision": study.revision,
            "analysis_input_digest": result["analysis_input_digest"], "outputs": outputs,
        }))
        directory.rename(destination)
    return str(destination), {node: str(destination / relative) for node, relative in outputs.items()}


async def finalize_studies(run_id: str) -> None:
    studies_repository = get_listening_studies_repository()
    participant_repository = get_participant_repository()
    snapshots = AnalysisSnapshotsRepository()
    studies = await studies_repository.for_run(run_id)
    if not studies or any(study.status == StudyStatus.COLLECTING for study in studies):
        raise ValueError("All listening studies must be closed before finalization")
    for study in studies:
        if study.status == StudyStatus.FINALIZED:
            continue
        result = await study_results(study, participant_repository, snapshots)
        rows = await response_rows(study, participant_repository)
        sessions = await session_metadata(study, participant_repository)
        directory, outputs = await asyncio.to_thread(publish_final_artifacts, study, result, rows, sessions)
        if not await studies_repository.mark_finalized(study.id, study.revision, directory, outputs):
            raise RuntimeError("Frozen study changed during finalization")


async def queue_finalization_if_ready(run_id: str, *, retry: bool = False) -> bool:
    from plc_platform_backend.runs.runs_models import RunStatus
    from plc_platform_backend.runs.runs_repository import get_runs_repository
    from plc_platform_backend.runs.runs_service import _transition_run_status
    from plc_platform_backend.runs.runs_actors import finalize_listening_run
    from plc_platform_backend.commons.redis_client import get_redis_client

    studies = await get_listening_studies_repository().for_run(run_id)
    if not studies or any(study.status == StudyStatus.COLLECTING for study in studies):
        return False
    repository = get_runs_repository()
    run = await repository.get_run(run_id)
    if run is None:
        return False
    from plc_platform_backend.modules.modules_models import ModuleType
    expected_studies = {
        (human_index, simulator_index)
        for human_index, module in enumerate(run.modules[ModuleType.OutputAnalyser]) if module.name == "HumanCalculator"
        for simulator_index in range(len(run.modules[ModuleType.PacketLossSimulator]))
    }
    if {(study.human_module_index, study.simulator_module_index) for study in studies} != expected_studies:
        return False
    expected = RunStatus.FAILED if retry else RunStatus.AWAITING_INPUT
    queued = await _transition_run_status(run_id, run.name, expected, RunStatus.QUEUED, repository, get_redis_client())
    if queued is None:
        return False
    try:
        finalize_listening_run.send(run_id)
    except Exception:
        await _transition_run_status(run_id, run.name, RunStatus.QUEUED, expected, repository, get_redis_client())
        raise
    return True
