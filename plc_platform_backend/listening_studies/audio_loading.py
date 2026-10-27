"""Deployment-owned limits shared by study preparation and participant playback."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MIB = 1024 * 1024


class AudioLoadingLimits(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LISTENING_AUDIO_")

    # Decoded float32 audio retained for ONE trial, not the entire study.
    # Browser/decoder overhead is additional; this is not a total memory limit.
    budget_mib: int = Field(default=256, ge=1, le=1024)
    timeout_seconds: int = Field(default=120, ge=5, le=600)

    @property
    def max_decoded_bytes(self) -> int:
        return self.budget_mib * MIB

    def public_limits(self) -> dict[str, int]:
        return {
            "max_decoded_bytes": self.max_decoded_bytes,
            "timeout_seconds": self.timeout_seconds,
        }


class AudioLoadingBudgetError(ValueError):
    """Safe, actionable preparation feedback without private paths or labels."""

    def __init__(self, trial_number: int, required_bytes: int, duration: float, buffers: int, limit_bytes: int):
        self.user_message = (
            f"Listening trial {trial_number} requires {required_bytes / MIB:.2f} MiB of decoded audio "
            f"({duration:.1f} seconds, {buffers} audio buffers); "
            f"the configured limit is {limit_bytes / MIB:g} MiB."
        )
        self.guidance = (
            "Use shorter input tracks/excerpts or fewer PLC conditions, or ask the deployment "
            "administrator to increase LISTENING_AUDIO_BUDGET_MIB on both backend and workers. "
            "Browser decoding needs additional memory beyond this budget."
        )
        super().__init__(self.user_message)


def validate_study_audio_budget(study, limits: AudioLoadingLimits) -> None:
    """Check actual prepared float32 shapes before exporting/publishing a study.

    Explicit and hidden reference share one buffer. Count each PLC and the
    anchor, but never sum repetitions or different tracks loaded sequentially.
    """
    material = {}
    for bundle in study.bundles:
        for excerpt in bundle.excerpts:
            material.setdefault(excerpt.definition.id, {})[bundle.condition_id] = excerpt
    for number, trial in enumerate(study.definition.trials, start=1):
        excerpts = material[trial.excerpt.id]
        first = next(iter(excerpts.values()))
        audio = [first.reference, first.anchor] + [
            excerpts[condition.id].reconstructed
            for condition in trial.conditions if condition.role == "plc"
        ]
        required_bytes = sum(clip.size * 4 for clip in audio)
        if required_bytes > limits.max_decoded_bytes:
            raise AudioLoadingBudgetError(
                number, required_bytes, len(first.reference) / 48000,
                len(audio), limits.max_decoded_bytes,
            )
