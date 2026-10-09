"""Full preparation -> collection -> frozen run-specific JSON, using real audio."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from plctestbench.file_wrapper import DataFile

from tests.listening_studies.test_preparation import prepared_run
from plc_platform_backend.listening_studies.finalization import publish_final_artifacts, queue_finalization_if_ready
from plc_platform_backend.listening_studies.participant_models import CheckpointSave, ConsentAcknowledgement, DraftRating, InstructionsUpdate, SessionExclusion, StudyClose
from plc_platform_backend.listening_studies.participant_repository import ParticipantRepository
from plc_platform_backend.listening_studies.participant_service import ParticipantService, token_digest
from plc_platform_backend.listening_studies.results_service import AnalysisSnapshotsRepository, response_rows, session_metadata, study_results
from plc_platform_backend.listening_studies.studies_models import StudyStatus
from plc_platform_backend.listening_studies.studies_repository import ListeningStudiesRepository
from plc_platform_backend.listening_studies.studies_service import prepare_studies
from plc_platform_backend.runs.runs_models import RunDocument, RunStatus

mongomock_motor = pytest.importorskip("mongomock_motor")


@pytest.fixture
def prepared(tmp_path):
    run, bench = prepared_run(tmp_path)
    for root in bench.data_manager.root_nodes:
        for node in root.outputs:
            DataFile(node.get_data(), node.get_file().get_path())
    documents = prepare_studies(run, bench, str(tmp_path))
    database = mongomock_motor.AsyncMongoMockClient().database
    studies = object.__new__(ListeningStudiesRepository)
    studies.collection = database.listening_studies
    participants = object.__new__(ParticipantRepository)
    participants.collection = database.listening_studies
    participants.invitations = database.listening_invitations
    participants.checkpoints = database.listening_checkpoints
    snapshots = object.__new__(AnalysisSnapshotsRepository)
    snapshots.collection = database.listening_analysis_snapshots
    async def store():
        for document in documents:
            await studies.save_prepared(document)
    asyncio.run(store())
    return run, documents, ParticipantService(studies, participants), snapshots


async def complete(service, study_id, request_id, primary_score):
    link = (await service.create_invitations(study_id, 1))[0]
    token = parse_qs(urlsplit(link["path"]).fragment)["invite"][0]
    digest = token_digest(token)
    study = await service.study(study_id)
    await service.consent(digest, ConsentAcknowledgement(request_id=f"consent-{request_id}", expected_revision=0, consent_version=study.consent_version, accepted=True))
    ratings = [DraftRating(trial_id=trial["id"], condition_id=condition["id"], answered=True,
        score=100 if condition["role"] == "hidden_reference" else 10 if condition["role"] == "anchor" else primary_score)
        for trial in study.definition["trials"] for condition in trial["conditions"]]
    await service.save(digest, CheckpointSave(request_id=request_id, expected_revision=1,
        current_trial_id=study.definition["trials"][-1]["id"], responses=ratings, complete=True,
        audio_sample_rate=48000, browser="test"))
    return link["invitation_id"]


def test_frozen_outputs_are_run_scoped_idempotent_and_keep_zero_and_excluded_raw(prepared):
    _, documents, service, snapshots = prepared
    async def scenario():
        study_id = documents[0].id
        included = await complete(service, study_id, "first", 0)
        excluded = await complete(service, study_id, "second", 80)
        study = await service.study(study_id)
        await service.exclude(study_id, excluded, SessionExclusion(expected_study_revision=study.revision, excluded=True, reason="=unsafe spreadsheet formula"))
        provisional = await study_results(await service.study(study_id), service.participants, snapshots)
        assert provisional["provisional"]
        assert "quality_control_analysis" not in provisional["analysis"]
        assert provisional["counts"]["included"] == 1
        assert all(condition["mean"] == 0 for condition in provisional["analysis"]["conditions"])
        study = await service.study(study_id)
        await service.close(study_id, StudyClose(expected_revision=study.revision))
        frozen = await service.study(study_id)
        assert frozen.closed_at is not None
        rows = await response_rows(frozen, service.participants)
        metadata = await session_metadata(frozen, service.participants)
        assert len(rows) == 16 and len(metadata) == 2
        assert all(row["audio_context_sample_rate"] == 48000 for row in rows)
        assert all(session["consent_acknowledged_at"] for session in metadata)
        result = await study_results(frozen, service.participants, snapshots)
        assert result["included_sessions"] == [included]
        quality = result["analysis"]["quality_control_analysis"]
        assert quality["exploratory"] is True
        assert quality["participant_count"] == 1
        assert quality["response_count"] == len([row for row in rows if row["included"]])
        assert {condition["role"] for condition in quality["conditions"]} == {"plc", "anchor", "hidden_reference"}
        assert quality["inference"]["reason"] == "mixed_effects_requires_at_least_five_participants"
        before = {source.source_path: Path(source.source_path).read_bytes() for source in frozen.sources}
        directory, outputs = publish_final_artifacts(frozen, result, rows, metadata)
        assert publish_final_artifacts(frozen, result, rows, metadata) == (directory, outputs)
        with pytest.raises(RuntimeError, match="conflict"):
            publish_final_artifacts(frozen.model_copy(update={"revision": frozen.revision + 1}), result, rows, metadata)
        assert all(Path(path).read_bytes() == content for path, content in before.items())
        for path in outputs.values():
            payload = json.loads(Path(path).read_text())
            assert payload["status"] == "finalized" and "responses" not in payload
            assert payload["excerpts"][0]["mean"] == 0
            assert payload["excerpts"][0]["response_count"] == 1
        published = json.loads((Path(directory) / "results.json").read_text())
        assert published["status"] == "finalized"
        assert published["analysis"]["quality_control_analysis"] == quality
        assert "'=unsafe spreadsheet formula" in (Path(directory) / "responses.csv").read_text()
        raw = json.loads((Path(directory) / "responses.json").read_text())
        assert any(row["score"] == 80 and row["excluded"] and not row["included"] for row in raw)
        assert await service.studies.mark_finalized(study_id, frozen.revision, directory, outputs)
        assert not await service.studies.mark_finalized(study_id, frozen.revision, directory, outputs)
        persisted = await service.study(study_id)
        assert persisted.status == StudyStatus.FINALIZED and persisted.finalized_at is not None
    asyncio.run(scenario())


def test_empty_confirmed_closure_keeps_unobserved_means_null(prepared):
    _, documents, service, snapshots = prepared
    async def scenario():
        study_id = documents[0].id
        study = await service.study(study_id)
        await service.close(study_id, StudyClose(expected_revision=study.revision, confirm_empty=True))
        frozen = await service.study(study_id)
        result = await study_results(frozen, service.participants, snapshots)
        directory, outputs = publish_final_artifacts(frozen, result, [], [])
        assert Path(directory).is_dir()
        assert all(json.loads(Path(path).read_text())["excerpts"][0]["mean"] is None for path in outputs.values())
    asyncio.run(scenario())


def test_preparation_mutation_is_rejected(prepared):
    _, documents, service, snapshots = prepared
    async def scenario():
        study = documents[0].model_copy(update={"status": StudyStatus.CLOSED})
        result = await study_results(study, service.participants, snapshots)
        source = study.sources[0]
        data = DataFile(path=source.source_path).get_data()
        data.excerpts[0].reconstructed[0] = 1
        DataFile(data, source.source_path)
        with pytest.raises(RuntimeError, match="changed after"):
            publish_final_artifacts(study, result, [], [])
        assert not (Path(study.artifact_directory) / "finalized").exists()
    asyncio.run(scenario())


def test_instruction_edit_is_locked_after_first_invitation(prepared):
    _, documents, service, _ = prepared
    async def scenario():
        study_id = documents[0].id
        await service.update_instructions(study_id, InstructionsUpdate(expected_revision=0, instructions="Neutral instructions", consent="Local research consent"))
        study = await service.study(study_id)
        assert study.consent_version == 2
        await complete(service, study_id, "first", 40)
        with pytest.raises(HTTPException) as error:
            await service.update_instructions(study_id, InstructionsUpdate(expected_revision=study.revision, instructions="Cannot change", consent="Cannot change"))
        assert getattr(error.value, "status_code", None) == 409
    asyncio.run(scenario())


def test_queue_requires_every_configured_study_closed_and_rolls_back_broker_failure(prepared, monkeypatch):
    run, documents, service, _ = prepared
    import plc_platform_backend.listening_studies.finalization as module
    import plc_platform_backend.runs.runs_repository as runs_repository
    import plc_platform_backend.commons.redis_client as redis_module
    for key, value in {"MONGO_INITDB_ROOT_USERNAME": "test", "MONGO_INITDB_ROOT_PASSWORD": "test", "PLC_ROOT_FOLDER": documents[0].artifact_directory, "PLUGINS_DIRECTORY": documents[0].artifact_directory, "REDIS_URL": "redis://localhost:6379/0"}.items():
        monkeypatch.setenv(key, value)
    from plc_platform_backend.actors import finalize_listening_run

    documents = [document.model_copy(update={"status": StudyStatus.CLOSED}) for document in documents]
    study_repository = SimpleNamespace(for_run=AsyncMock(return_value=documents))
    running_document = RunDocument(**run.model_copy(update={"status": RunStatus.AWAITING_INPUT}).model_dump())
    repository = SimpleNamespace(get_run=AsyncMock(return_value=running_document), transition_status=AsyncMock(return_value=running_document))
    monkeypatch.setattr(module, "get_listening_studies_repository", lambda: study_repository)
    monkeypatch.setattr(runs_repository, "get_runs_repository", lambda: repository)
    monkeypatch.setattr(redis_module, "get_redis_client", lambda: SimpleNamespace(publish=AsyncMock()))
    send = MagicMock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr(finalize_listening_run, "send", send)
    async def scenario():
        study_repository.for_run.return_value = documents[:-1]
        assert not await queue_finalization_if_ready(run.id)
        assert not repository.transition_status.called
        study_repository.for_run.return_value = documents
        with pytest.raises(RuntimeError, match="broker unavailable"):
            await queue_finalization_if_ready(run.id)
        assert [call.args[1:] for call in repository.transition_status.await_args_list] == [
            (RunStatus.AWAITING_INPUT, RunStatus.QUEUED), (RunStatus.QUEUED, RunStatus.AWAITING_INPUT),
        ]
    asyncio.run(scenario())
