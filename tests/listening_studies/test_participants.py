import asyncio
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")
from pydantic import ValidationError

from plc_platform_backend.listening_studies.participant_controller import COOKIE_PREFIX, get_participant_service, router
from plc_platform_backend.listening_studies.participant_models import CheckpointSave, ConsentAcknowledgement, DraftRating, SessionExclusion, StudyClose
from plc_platform_backend.listening_studies.participant_repository import ParticipantRepository
from plc_platform_backend.listening_studies.participant_service import ParticipantService, token_digest
from plc_platform_backend.listening_studies.studies_models import ListeningStudyDocument, StudyStatus
from plc_platform_backend.listening_studies.studies_repository import ListeningStudiesRepository


@pytest.fixture
def service(tmp_path):
    database = mongomock_motor.AsyncMongoMockClient().database
    studies = object.__new__(ListeningStudiesRepository)
    studies.collection = database.listening_studies
    participants = object.__new__(ParticipantRepository)
    participants.collection = database.listening_studies
    participants.invitations = database.listening_invitations
    participants.checkpoints = database.listening_checkpoints
    definition = {"schema_version": 1, "test_type": "mushra", "trials": [
        {"id": f"trial-{index}", "conditions": [{"id": name} for name in ("plc-0", "reference", "anchor35")]}
        for index in range(2)
    ]}
    study = ListeningStudyDocument(
        id="study", run_id="a" * 24, human_module_index=0, simulator_module_index=0,
        status=StudyStatus.COLLECTING, created=datetime.utcnow(), definition=definition,
        conditions=[], warnings=[], trial_count=2,
        artifact_directory=str(tmp_path), audio_files=[], sources=[],
    )
    asyncio.run(studies.save_prepared(study))
    return ParticipantService(studies, participants)


async def invite(service):
    link = (await service.create_invitations("study", 1))[0]
    token = parse_qs(urlsplit(link["path"]).fragment)["invite"][0]
    return token, link["invitation_id"]


async def acknowledge(service, token):
    return await service.consent(token_digest(token), ConsentAcknowledgement(
        request_id="consent", expected_revision=0, consent_version=1, accepted=True,
    ))


def save_request(revision=1, score=0, request_id="save", complete=False):
    return CheckpointSave(request_id=request_id, expected_revision=revision, current_trial_id="trial-0", complete=complete,
        responses=[DraftRating(trial_id="trial-0", condition_id="plc-0", score=score, answered=True)])


def test_delete_unused_invitation_removes_row_token_and_checkpoints(service):
    async def scenario():
        token, session_id = await invite(service)
        assert await service.participants.delete_unused_invitation('study', session_id)
        study = await service.study('study')
        assert session_id not in study.sessions
        assert study.invitations_created
        assert await service.participants.invitation(token_digest(token)) is None
        assert await service.participants.checkpoints.count_documents({'session_id': session_id}) == 0
        with pytest.raises(HTTPException):
            await service.checkpoint(token_digest(token))
        assert not await service.participants.publish_checkpoint('study', session_id, 0, 'late', False, [])
        assert not await service.participants.update_instructions('study', study.revision, 'Changed', 'Changed')
    asyncio.run(scenario())


def test_delete_protects_drafts_completed_and_closed_sessions(service):
    async def scenario():
        token, session_id = await invite(service)
        await acknowledge(service, token)
        assert not await service.participants.delete_unused_invitation('study', session_id)
        assert session_id in (await service.study('study')).sessions
        _, completed = await invite(service)
        await service.participants.collection.update_one({'_id': 'study'}, {'$set': {f'sessions.{completed}.status': 'completed'}})
        assert not await service.participants.delete_unused_invitation('study', completed)
        _, unused = await invite(service)
        study = await service.study('study')
        await service.participants.close('study', study.revision)
        assert not await service.participants.delete_unused_invitation('study', unused)
    asyncio.run(scenario())


def test_invitation_consent_zero_score_and_exact_resume(service):
    async def scenario():
        token, session_id = await invite(service)
        initial = await service.checkpoint(token_digest(token))
        with pytest.raises(HTTPException) as error:
            await service.save(token_digest(token), save_request(0))
        assert error.value.status_code == 403
        consent = await acknowledge(service, token)
        assert consent["revision"] == 1
        saved = await service.save(token_digest(token), save_request())
        resumed = await service.checkpoint(token_digest(token))
        assert resumed["session_id"] == session_id
        assert resumed["trial_order"] == initial["trial_order"]
        assert resumed["condition_orders"] == initial["condition_orders"]
        assert resumed["responses"][0]["score"] == 0
        assert resumed["responses"][0]["answered"]
        assert saved["revision"] == resumed["revision"] == 2
        assert await service.participants.invitation(token) is None  # raw tokens are not stored
        duplicate = await service.save(token_digest(token), save_request())
        assert duplicate["revision"] == 2
        with pytest.raises(HTTPException) as error:
            await service.save(token_digest(token), save_request(score=10))
        assert error.value.status_code == 409
        with pytest.raises(HTTPException) as error:
            await service.save(token_digest(token), save_request(score=20, request_id="other-tab"))
        assert error.value.status_code == 409
    asyncio.run(scenario())


def test_completion_requires_every_rating_and_retains_advisory_flags(service):
    async def scenario():
        token, session_id = await invite(service)
        await acknowledge(service, token)
        with pytest.raises(HTTPException) as error:
            await service.save(token_digest(token), save_request(complete=True))
        assert error.value.status_code == 422
        ratings = [DraftRating(trial_id=f"trial-{trial}", condition_id=condition, score=score, answered=True)
            for trial in range(2) for condition, score in (("plc-0", 0), ("reference", 80), ("anchor35", 90))]
        request = CheckpointSave(request_id="complete", expected_revision=1, current_trial_id="trial-1", complete=True, responses=ratings)
        completed = await service.save(token_digest(token), request)
        assert completed["status"] == "completed"
        study = await service.study("study")
        head = study.sessions[session_id]
        assert head.flags and not head.excluded
        assert (await service.save(token_digest(token), request))["status"] == "completed"
        with pytest.raises(HTTPException) as error:
            await service.save(token_digest(token), save_request(2, request_id="late-edit"))
        assert error.value.status_code == 409
        closed = await service.close("study", StudyClose(expected_revision=study.revision))
        assert closed["included_sessions"] == 1
        frozen = await service.study("study")
        assert frozen.sessions[session_id].checkpoint_id == head.checkpoint_id
    asyncio.run(scenario())


def test_close_vs_save_uses_atomic_head_publication_not_staged_rows(service, monkeypatch):
    async def scenario():
        token, session_id = await invite(service)
        await acknowledge(service, token)
        publish = service.participants.publish_checkpoint
        async def close_before_publish(*args, **kwargs):
            study = await service.study("study")
            await service.participants.close("study", study.revision)
            return await publish(*args, **kwargs)
        monkeypatch.setattr(service.participants, "publish_checkpoint", close_before_publish)
        with pytest.raises(HTTPException) as error:
            await service.save(token_digest(token), save_request())
        assert error.value.status_code == 409
        frozen = await service.study("study")
        assert frozen.status == StudyStatus.CLOSED
        assert frozen.sessions[session_id].revision == 1
        checkpoint = await service.checkpoint(token_digest(token))
        assert checkpoint["responses"] == []
        assert await service.participants.checkpoints.count_documents({}) == 3  # unpublished row remains audit-only
    asyncio.run(scenario())


def test_empty_close_needs_confirmation_and_exclusions_keep_raw_data(service):
    async def scenario():
        token, session_id = await invite(service)
        await acknowledge(service, token)
        study = await service.study("study")
        with pytest.raises(HTTPException):
            await service.close("study", StudyClose(expected_revision=study.revision))
        await service.exclude("study", session_id, SessionExclusion(expected_study_revision=study.revision, excluded=True, reason="Owner review"))
        study = await service.study("study")
        assert study.sessions[session_id].excluded
        assert len(study.sessions[session_id].exclusion_audit) == 1
        closed = await service.close("study", StudyClose(expected_revision=study.revision, confirm_empty=True))
        assert closed["included_sessions"] == 0
        with pytest.raises(HTTPException):
            await service.create_invitations("study", 1)
    asyncio.run(scenario())


def test_revocation_denies_resume(service):
    async def scenario():
        token, session_id = await invite(service)
        assert await service.participants.revoke("study", session_id)
        with pytest.raises(HTTPException) as error:
            await service.checkpoint(token_digest(token))
        assert error.value.status_code == 403
    asyncio.run(scenario())


@pytest.mark.parametrize("score", [True, "50", float("nan"), float("inf"), -1, 101])
def test_scores_are_strict_finite_and_bounded(score):
    with pytest.raises(ValidationError):
        DraftRating(trial_id="t", condition_id="c", score=score, answered=True)


def test_session_scoped_cookies_do_not_cross_tabs_and_require_origin(service):
    async def invitations():
        return [await invite(service), await invite(service)]
    first, second = asyncio.run(invitations())
    inner = FastAPI()
    inner.include_router(router)
    inner.dependency_overrides[get_participant_service] = lambda: service
    application = FastAPI()
    application.mount("/api", inner)
    with TestClient(application, base_url="https://testserver") as client:
        assert client.post("/api/listening-participant/exchange", json={"token": first[0]}).status_code == 403
        for token, session in (first, second):
            response = client.post("/api/listening-participant/exchange", json={"token": token}, headers={"Origin": "https://testserver"})
            assert response.status_code == 200
            cookie = response.headers["set-cookie"]
            assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
            assert f"Path=/api/listening-participant/{session}" in cookie
        for _, session in (first, second):
            response = client.get(f"/api/listening-participant/{session}/checkpoint")
            assert response.status_code == 200 and response.json()["session_id"] == session
        url = f"/api/listening-participant/{first[1]}/checkpoint"
        assert client.get(url, headers={"Origin": "https://evil.testserver"}).status_code == 403
        assert client.get(url, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
        assert client.get(url, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        # The wrong token remains forbidden even if manually given another name.
        client.cookies.clear()
        client.cookies.set(COOKIE_PREFIX + first[1], second[0], path=f"/api/listening-participant/{first[1]}")
        assert client.get(f"/api/listening-participant/{first[1]}/checkpoint").status_code == 403


def test_resume_retains_all_reported_audio_rates_and_omitted_metadata(service):
    async def scenario():
        token, session_id = await invite(service)
        await acknowledge(service, token)
        first = save_request().model_copy(update={"audio_sample_rate": 44100, "browser": "first browser"})
        await service.save(token_digest(token), first)
        second = save_request(2, request_id="second-device").model_copy(update={"audio_sample_rate": 48000})
        saved = await service.save(token_digest(token), second)
        assert saved["audio_sample_rates"] == [44100, 48000]
        assert saved["browser"] == "first browser"
        ratings = [DraftRating(trial_id=f"trial-{trial}", condition_id=condition, answered=True, score=score)
            for trial in range(2) for condition, score in (("plc-0", 50), ("reference", 100), ("anchor35", 10))]
        completed = await service.save(token_digest(token), CheckpointSave(request_id="done", expected_revision=3, current_trial_id="trial-1", responses=ratings, complete=True))
        assert completed["audio_sample_rate"] == 48000
        head = (await service.study("study")).sessions[session_id]
        assert "audio_context_rate_differs_from_48000" in head.flags
    asyncio.run(scenario())
