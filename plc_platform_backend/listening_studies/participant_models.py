from math import isfinite
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, model_validator


MAX_RATINGS_PER_SESSION = 20000


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvitationCreate(RequestModel):
    count: StrictInt = Field(default=1, ge=1, le=100)


class InvitationExchange(RequestModel):
    token: str = Field(min_length=32, max_length=128)


class DraftRating(RequestModel):
    trial_id: str = Field(min_length=1, max_length=128)
    condition_id: str = Field(min_length=1, max_length=128)
    score: StrictFloat | None = None
    answered: StrictBool = False

    @model_validator(mode="after")
    def check_score(self):
        if self.answered and (self.score is None or not isfinite(self.score) or not 0 <= self.score <= 100):
            raise ValueError("Answered MUSHRA ratings require a finite score between 0 and 100")
        if not self.answered and self.score is not None:
            raise ValueError("Unanswered ratings must have a null score")
        return self


class CheckpointSave(RequestModel):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: StrictInt = Field(ge=0)
    current_trial_id: str = Field(min_length=1, max_length=128)
    responses: list[DraftRating] = Field(max_length=MAX_RATINGS_PER_SESSION)
    complete: StrictBool = False
    audio_sample_rate: StrictInt | None = Field(default=None, ge=8000, le=192000)
    browser: str | None = Field(default=None, max_length=256)


class ConsentAcknowledgement(RequestModel):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: StrictInt = Field(ge=0)
    consent_version: StrictInt = Field(ge=1)
    accepted: StrictBool


class StudyClose(RequestModel):
    expected_revision: StrictInt = Field(ge=0)
    confirm_empty: StrictBool = False


class InstructionsUpdate(RequestModel):
    expected_revision: StrictInt = Field(ge=0)
    instructions: str = Field(min_length=1, max_length=10000)
    consent: str = Field(min_length=1, max_length=10000)


class SessionExclusion(RequestModel):
    expected_study_revision: StrictInt = Field(ge=0)
    excluded: StrictBool
    reason: str = Field(min_length=1, max_length=1000)
