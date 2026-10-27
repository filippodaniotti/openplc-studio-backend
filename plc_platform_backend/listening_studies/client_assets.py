"""Pinned, locally served listening client; no separate listening server."""
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope


class RevalidatingStaticFiles(StaticFiles):
    """Revalidate the iframe and its assets rather than using heuristic freshness."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def mount_listening_client(app: FastAPI) -> None:
    directory = Path(__file__).resolve().parent.parent / "static" / "listening-client"
    app.mount("/listening-client", RevalidatingStaticFiles(directory=directory, html=True), name="listening-client")
