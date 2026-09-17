from typing import Literal
from pydantic import BaseModel


class RunCompletionMessage(BaseModel):
    # Message sent when a run completes
    type: Literal["run.complete"] = "run.complete"
    run_name: str
    success: bool
