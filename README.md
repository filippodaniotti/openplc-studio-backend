# OpenPLC Studio Backend

![OpenPLC Studio banner](https://raw.githubusercontent.com/CIMIL/openplc-studio/main/assets/banner.png)

[![Unit tests](https://github.com/filippodaniotti/openplc-studio-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/filippodaniotti/openplc-studio-backend/actions/workflows/tests.yml)
[![Build and publish](https://github.com/filippodaniotti/openplc-studio-backend/actions/workflows/publish-image.yml/badge.svg)](https://github.com/filippodaniotti/openplc-studio-backend/actions/workflows/publish-image.yml)
[![Docker pulls](https://img.shields.io/docker/pulls/cimil/openplc-studio-backend?logo=docker&label=Docker%20pulls)](https://hub.docker.com/r/cimil/openplc-studio-backend)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker image](https://img.shields.io/badge/container-linux%2Famd64-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/cimil/openplc-studio-backend)

Backend service for **OpenPLC Studio**, a platform for configuring, executing, and analysing PLC audio-codec test runs. This repository contains the HTTP API, asynchronous processing workers, integration with the PLC Testbench library, and the production container image.

> This README is about the backend application. For the complete, ready-to-run platform and its deployment documentation, see [OpenPLC Studio](https://github.com/CIMIL/openplc-studio).

## What it does

- Accepts and manages audio assets used as test inputs.
- Discovers codec and testbench modules and exposes their configurable parameters.
- Validates, persists, and executes test-run configurations.
- Runs long-lived processing work asynchronously and reports progress in real time.
- Serves the documentation generated from the installed `plctestbench` package at `/plctestbench-docs/`.
- Stores run metadata in MongoDB and generated artifacts on persistent storage.
- Exports run configurations and generated assets for reproducibility and inspection.

## Architecture

The service is designed to run behind the OpenPLC Studio reverse proxy alongside the frontend, MongoDB, Redis, and one or more worker processes.

```text
browser ── /api, /ws ──> reverse proxy ──> FastAPI API
                                             │
                              MongoDB <──────┼──────> File system
                                             │
                                         Redis broker
                                             │
                                      Dramatiq workers
                                             │
                                    PLC Testbench library
```

The API handles request validation, metadata, uploads, and run orchestration. CPU-intensive testbench work is queued through Redis and executed by Dramatiq workers, keeping the API responsive. Clients subscribe to `/ws/runs` for per-run progress and completion events.

## Technology

| Area            | Technology                                               |
| --------------- | -------------------------------------------------------- |
| API             | Python 3.11, FastAPI, Pydantic                           |
| Persistence     | MongoDB via Motor                                        |
| Background jobs | Dramatiq with Redis                                      |
| Test execution  | [`plctestbench`](https://github.com/CIMIL/plc-testbench) |
| Packaging       | `uv`, Hatchling, Docker Buildx                           |

## Logging

The API, Uvicorn access/server logs, application modules, and Dramatiq workers use one standard logging configuration. Records are written to stderr as readable, non-colored text with a UTC timestamp, severity, process/thread context, logger name, and message.

Set `LOG_LEVEL` to `trace`, `debug`, `info`, `warning`, `error`, or `critical`; the default is `info`. Invalid values stop the API or worker during startup with a configuration error.

## Container image

The published image is [`cimil/openplc-studio-backend`](https://hub.docker.com/r/cimil/openplc-studio-backend). It is a multi-stage, non-root production image with the API health endpoint exposed on port `8000`.

| Tag            | Intended use                                   |
| -------------- | ---------------------------------------------- |
| `latest`       | Current build from `master`                    |
| `prod`         | Production branch alias from `master`          |
| `staging`      | Staging branch alias from `staging`            |
| `sha-<commit>` | Immutable build for a specific source revision |

Production images target **`linux/amd64`**. They are consumed by the [OpenPLC Studio deployment repository](https://github.com/CIMIL/openplc-studio), which supplies the surrounding services, routing, persistent volumes, and runtime configuration.

The image build runs `plctestbench-docs build --strict --profile embedded` against the locked package version and copies only the generated static site into the runtime image. This profile uses the application-specific introduction and excludes the standalone package introduction, getting-started pages, and repository links. Set `PLCTESTBENCH_DOCS_DIRECTORY` only when serving a site from a non-default location. If the site is missing, only the documentation route returns `503`; the API continues to start normally. In the local Compose workflow, rebuild the backend image after changing documentation sources; restarting the API does not regenerate the site.

## Listening audio loading limits

Set these environment variables **identically on the API and workers**:

| Variable | Default | Valid range | Meaning |
| --- | --- | --- | --- |
| `LISTENING_AUDIO_BUDGET_MIB` | `256` | `1`–`1024` | Decoded float32 audio retained for one listening trial. |
| `LISTENING_AUDIO_TIMEOUT_SECONDS` | `120` | `5`–`600` | Loading timeout per audio file. |

Study preparation checks the actual prepared sample/channel counts before
publishing material or allowing invitations. The explicit/hidden reference is
counted once, alongside every PLC condition and the anchor; different tracks and
repetitions are not summed. An oversized trial fails the run with required and
allowed sizes plus actionable guidance in run progress.

The same limits are delivered to the player in participant checkpoints, including
existing studies and invitations. Browser guards remain active; no audio is
silently cropped or downmixed. Device resampling may increase actual decoded
size. Decoding, downloads and browser overhead need memory **in addition** to the
retained-audio budget. Increasing it does not guarantee playback on every device.

For local Compose, optionally add the variables to the root `dev.env`, then run
`make restart` (when no computation is running) and refresh the participant page.
Both services receive the variables through the common Compose environment.
No dependency/image rebuild or existing-study regeneration is needed for this
source-only change. A plain `docker compose restart` does not apply changed
container environment values; recreate the containers after changing them.

## Repository layout

```text
plc_platform_backend/
├── assets/       # Upload and artifact handling
├── modules/      # Testbench module discovery and configuration
├── runs/         # Run lifecycle, persistence, workers, and WebSockets
├── db/           # MongoDB integration
└── commons/      # Shared configuration and utilities
.docker/          # Local and production container definitions
```

## Delivery

GitHub Actions builds on pushes to `master` and `staging`, publishes branch
aliases plus an immutable `sha-<commit>` tag, and reports the resulting image
digest to the private infrastructure repository. Production and staging remain
pinned to immutable digests even though mutable aliases are published for
operator convenience. Image metadata follows the OCI image-label convention.

**Keywords:** `OpenPLC` · `PLC` · `audio codec` · `testbench` · `FastAPI` · `MongoDB` · `Redis` · `Dramatiq` · `WebSocket` · `Docker` · `CI/CD`
