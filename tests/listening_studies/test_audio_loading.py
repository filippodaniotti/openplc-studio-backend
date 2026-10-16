"""Audio budgets count one trial's decoded float32 buffers, not study length."""
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError
from plctestbench.human_listening import (
    ConditionDefinition, ExcerptDefinition, HumanCalculatorData,
    ListeningStudy, ListeningTestDefinition, PreparedExcerpt, TrialDefinition,
)

from plc_platform_backend.listening_studies.audio_loading import (
    MIB, AudioLoadingBudgetError, AudioLoadingLimits, validate_study_audio_budget,
)
from plc_platform_backend.listening_studies.participant_service import ParticipantService


@pytest.fixture(autouse=True)
def clean_audio_environment(monkeypatch):
    monkeypatch.delenv("LISTENING_AUDIO_BUDGET_MIB", raising=False)
    monkeypatch.delenv("LISTENING_AUDIO_TIMEOUT_SECONDS", raising=False)


def study_with_audio(frames, channels=2, plc_count=2, repetitions=1, track_count=1):
    # Logical full-size arrays without allocating hundreds of MiB in fixtures.
    audio = np.broadcast_to(np.float32(0), (frames, channels))
    bundles, trials = [], []
    conditions = tuple(ConditionDefinition(f"plc-{i}", "plc") for i in range(plc_count)) + (
        ConditionDefinition("reference", "hidden_reference"), ConditionDefinition("anchor35", "anchor"),
    )
    for track in range(track_count):
        excerpt = ExcerptDefinition(f"excerpt-{track}", f"track-{track}", "mask", 0, frames, 0, 0, 1, 48000)
        material = PreparedExcerpt(excerpt, audio, audio, audio)
        bundles.extend(HumanCalculatorData(f"plc-{i}", (material,), iterations=repetitions) for i in range(plc_count))
        trials.extend(TrialDefinition(f"trial-{track}-{r}", excerpt, r, conditions) for r in range(repetitions))
    return ListeningStudy(ListeningTestDefinition("test", tuple(trials)), tuple(bundles))


def test_defaults_and_environment_are_shared_with_existing_checkpoints(monkeypatch):
    assert AudioLoadingLimits().public_limits() == {"max_decoded_bytes": 256 * MIB, "timeout_seconds": 120}
    monkeypatch.setenv("LISTENING_AUDIO_BUDGET_MIB", "128")
    monkeypatch.setenv("LISTENING_AUDIO_TIMEOUT_SECONDS", "180")
    study = SimpleNamespace(id="s", status="collecting", definition={}, instructions="", consent="")
    head = SimpleNamespace(session_id="p", status="draft", revision=1)
    checkpoint = {"payload": {"responses": [], "audio_loading_limits": {"max_decoded_bytes": 1}}}
    public = ParticipantService.public_checkpoint(study, head, checkpoint)
    assert public["audio_loading_limits"] == {"max_decoded_bytes": 128 * MIB, "timeout_seconds": 180}
    assert checkpoint["payload"]["audio_loading_limits"] == {"max_decoded_bytes": 1}  # no persistence migration


@pytest.mark.parametrize("variable,value", [
    ("LISTENING_AUDIO_BUDGET_MIB", "0"), ("LISTENING_AUDIO_BUDGET_MIB", "1025"),
    ("LISTENING_AUDIO_BUDGET_MIB", "1.5"), ("LISTENING_AUDIO_BUDGET_MIB", "unlimited"),
    ("LISTENING_AUDIO_TIMEOUT_SECONDS", "0"), ("LISTENING_AUDIO_TIMEOUT_SECONDS", "601"),
])
def test_invalid_deployment_limits_are_rejected(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError):
        AudioLoadingLimits()


def test_45_second_stereo_trial_exceeds_old_budget_but_fits_default():
    study = study_with_audio(45 * 48000)
    validate_study_audio_budget(study, AudioLoadingLimits())
    with pytest.raises(AudioLoadingBudgetError) as caught:
        validate_study_audio_budget(study, AudioLoadingLimits(budget_mib=64))
    assert "65.92 MiB" in caught.value.user_message
    assert "45.0 seconds, 4 audio buffers" in caught.value.user_message
    assert "64 MiB" in caught.value.user_message
    assert "LISTENING_AUDIO_BUDGET_MIB" in caught.value.guidance


def test_exact_boundary_counts_reference_once_and_not_repetitions_or_tracks():
    # Four buffers, each 1 MiB. Explicit and hidden reference are the same buffer.
    study = study_with_audio(MIB // 8, repetitions=10, track_count=3)
    validate_study_audio_budget(study, AudioLoadingLimits(budget_mib=4))
    with pytest.raises(AudioLoadingBudgetError):
        validate_study_audio_budget(study_with_audio(MIB // 8 + 1), AudioLoadingLimits(budget_mib=4))


def test_channels_and_plc_count_affect_required_memory():
    frames = 40 * 48000
    validate_study_audio_budget(study_with_audio(frames, channels=1), AudioLoadingLimits(budget_mib=64))
    validate_study_audio_budget(study_with_audio(frames, plc_count=1), AudioLoadingLimits(budget_mib=64))
    with pytest.raises(AudioLoadingBudgetError):
        validate_study_audio_budget(study_with_audio(frames, plc_count=3), AudioLoadingLimits(budget_mib=64))
