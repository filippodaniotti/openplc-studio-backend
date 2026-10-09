"""Pinned, locally served listening client; no separate listening server."""
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def mount_listening_client(app: FastAPI) -> None:
    directory = Path(__file__).resolve().parent.parent / "static" / "listening-client"
    app.mount("/listening-client", StaticFiles(directory=directory, html=True), name="listening-client")
