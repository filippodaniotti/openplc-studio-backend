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

## Container image

The published image is [`cimil/openplc-studio-backend`](https://hub.docker.com/r/cimil/openplc-studio-backend). It is a multi-stage, non-root production image with the API health endpoint exposed on port `8000`.

| Tag            | Intended use                                   |
| -------------- | ---------------------------------------------- |
| `latest`       | Current build from `master`                    |
| `sha-<commit>` | Immutable build for a specific source revision |

Production images target **`linux/amd64`**. They are consumed by the [OpenPLC Studio deployment repository](https://github.com/CIMIL/openplc-studio), which supplies the surrounding services, routing, persistent volumes, and runtime configuration.

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

A GitHub Actions workflow builds the production Docker image on every push to `master` and publishes both `latest` and an immutable `sha-<commit>` tag to Docker Hub. Image metadata follows the OCI image-label convention.

**Keywords:** `OpenPLC` · `PLC` · `audio codec` · `testbench` · `FastAPI` · `MongoDB` · `Redis` · `Dramatiq` · `WebSocket` · `Docker` · `CI/CD`
