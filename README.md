# plc-platform-backend

FastAPI HTTP/WebSocket API and dramatiq workers for the PLC testbench platform.

## Production image

The production image is `plc-platform-backend/.docker/Dockerfile.prod`. The build
context **must be the repository root** because the image vendors the sibling
`plc-testbench` package (path dependency) and its `dl_models` folder:

```bash
docker build \
  -f plc-platform-backend/.docker/Dockerfile.prod \
  -t plc-platform/backend:prod \
  .
```

The image is CPU-only and runs as the non-root user `app` (uid/gid `1000`).
The same image serves both production roles; the role is selected through the
`SERVICE_ROLE` environment variable (or the first container argument).

### API

```bash
docker run --rm \
  -p 8000:8000 \
  -e MONGO_INITDB_ROOT_USERNAME=root \
  -e MONGO_INITDB_ROOT_PASSWORD=root \
  -e REDIS_URL=redis://redis:6379 \
  -e PLC_ROOT_FOLDER=./artifacts-storage \
  -e PLUGINS_DIRECTORY=./user-plugins \
  -v backend-artifacts:/app/backend/artifacts-storage \
  -v backend-plugins:/app/backend/user-plugins \
  plc-platform/backend:prod
```

The API listens on port `8000` and exposes `GET /health`.

### Worker

```bash
docker run --rm \
  -e SERVICE_ROLE=worker \
  -e MONGO_INITDB_ROOT_USERNAME=root \
  -e MONGO_INITDB_ROOT_PASSWORD=root \
  -e REDIS_URL=redis://redis:6379 \
  -e PLC_ROOT_FOLDER=./artifacts-storage \
  -e PLUGINS_DIRECTORY=./user-plugins \
  -v backend-artifacts:/app/backend/artifacts-storage \
  -v backend-plugins:/app/backend/user-plugins \
  plc-platform/backend:prod
```

### Healthcheck

The image ships a `HEALTHCHECK` that calls `/health` for the API role and is a
no-op for the worker role.

## Configuration

Environment variables (Pydantic settings, case-insensitive):

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `MONGO_INITDB_ROOT_USERNAME` | yes | — | MongoDB username |
| `MONGO_INITDB_ROOT_PASSWORD` | yes | — | MongoDB password |
| `REDIS_URL` | yes | — | Redis connection URL |
| `PLC_ROOT_FOLDER` | yes | — | Artifact storage root |
| `PLUGINS_DIRECTORY` | yes | — | User PLC plugin directory |
| `MONGO_HOST` | no | `mongo` | MongoDB host |
| `MONGO_PORT` | no | `27017` | MongoDB port |
| `MONGO_DATABASE` | no | `plc-testbench` | API MongoDB database |
| `API_HOST` | no | `0.0.0.0` | API bind address |
| `API_PORT` | no | `8000` | API bind port |
| `API_WORKERS` | no | `1` | Uvicorn worker processes |
| `FORWARDED_ALLOW_IPS` | no | `*` | Trusted reverse-proxy IPs |
| `LOG_LEVEL` | no | `info` | Uvicorn log level |
| `SERVICE_ROLE` | no | `api` | `api`, `worker` or `healthcheck` |

### Storage layout

Mount persistent volumes at:

- `/app/backend/artifacts-storage` — set `PLC_ROOT_FOLDER=./artifacts-storage`.
- `/app/backend/user-plugins` — set `PLUGINS_DIRECTORY=./user-plugins`.

`PLC_ROOT_FOLDER` must keep the `./<single-directory>` shape (relative to the
image working directory `/app/backend`): the artifact export code strips the
first two path segments, so an absolute or deeper path changes the exported
archive layout. Volumes must be writable by uid `1000`.
