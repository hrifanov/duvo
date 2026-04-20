# duvo — sandbox orchestration platform

Lightweight orchestration service that manages the lifecycle of isolated sandbox containers spun up on demand by AI agents. Built as a 5-step incremental exercise; each step is fully working and verifiable below.

## Architecture

Four processes coordinating through Redis and Docker:

```
┌──────────┐   LPUSH jobs    ┌──────────┐   docker run    ┌───────────┐
│ producer │ ──────────────> │ consumer │ ──────────────> │ sandbox-* │
└──────────┘   Redis list    └────┬─────┘                 │ container │
                                  │                       └───────────┘
                                  │ HSET sandbox:<id>
                                  ▼
                             ┌─────────┐
                             │  Redis  │ ◄──── reads ────┐
                             └─────────┘                 │
                                  ▲                      │
                                  │ HDEL / reconcile     │
                             ┌────┴─────┐          ┌─────┴────┐
                             │  reaper  │          │  viewer  │
                             └──────────┘          │ :8000    │
                                                   └──────────┘
```

- **producer.py** — emits random jobs (`http` or `browser`) every 3s
- **consumer.py** — pops jobs, spawns containers, writes state to Redis
- **viewer.py** — FastAPI observability service + dashboard
- **reaper.py** — enforces TTL, detects failures, reconciles drift

**Design principle:** Docker labels are the source of truth for "what's running." Redis holds enrichment (job context, timestamps, TTL). Viewer queries Docker live and joins Redis — never stale.

---

## Quickstart

**Requirements:** Docker Desktop running, Python 3.10+, ~200MB of image pulls on first run.

```bash
# 1. bootstrap
docker compose up -d redis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start all four processes (4 terminals, venv active in each):

```bash
# terminal A
python consumer.py

# terminal B
python producer.py

# terminal C
uvicorn viewer:app --port 8000

# terminal D
python reaper.py
```

Open the dashboard: **http://localhost:8000**

You should see sandboxes appearing every ~3s, a mix of `http` (green) and `browser` (orange), each with a live TTL countdown and a **release** button. Click any sandbox URL to open it in a new tab.

**Shutdown:** Ctrl+C all four processes. Consumer logs `removed N container(s), cleared M state key(s)`. Then `docker compose down` stops Redis.

---

## How to verify each step

Each step below is self-contained. Run the verify block from a new terminal with the 4 processes running (see Quickstart). Commands assume you're in the project root with venv active.

### Step 1 — Job queue (producer + consumer)

**What was built:** Redis list–backed queue. Producer serializes `{jobId, type, created_at}` as JSON and `LPUSH`es to `jobs:pending`. Consumer uses `BRPOP` (blocking, 5s timeout — no busy loop) and logs each job.

**Verify:**

```bash
# producer is emitting; consumer keeps up -> queue stays near 0
docker exec duvo-redis-1 redis-cli LLEN jobs:pending

# inject a job manually and see consumer pick it up
docker exec duvo-redis-1 redis-cli LPUSH jobs:pending \
  '{"jobId":"manual-1","type":"http","created_at":"2026-04-20T00:00:00Z"}'

# expect consumer log: "spawned http sandbox job=manual-1 url=..."
```

---

### Step 2 — Containerized sandboxes

**What was built:** Consumer spawns a real container per `http` job using `python:3-slim` running `python -m http.server 80`. Docker assigns a random host port (no port conflicts at scale). URL is logged and persisted.

**Verify:**

```bash
# see one sandbox per recent job, each with unique high port
docker ps --filter label=duvo.app=duvo \
  --format "table {{.Names}}\t{{.Ports}}"

# hit a sandbox's HTTP server through its mapped port
PORT=$(docker ps --filter label=duvo.type=http --format '{{.Ports}}' | head -1 | grep -oE ':[0-9]+->' | head -1 | tr -d ':>->')
curl -s http://localhost:$PORT/ | head -5
# expect: <!DOCTYPE HTML> ... Directory listing for /
```

---

### Step 3 — Multiple sandbox types (extensibility)

**What was built:** Added a `browser` type using `chromedp/headless-shell` with CDP (Chrome DevTools Protocol) exposed on container port 9222. Refactored to a `SandboxSpec` dataclass registry in `sandbox.py`.

**Adding a new sandbox type = one dict entry in `SPECS`.** Zero changes to the consumer or viewer. See `sandbox.py` lines 34–47.

**Verify:**

```bash
# both types running side by side
docker ps --filter label=duvo.app=duvo \
  --format "{{.Labels}}" | grep -oE "duvo.type=[a-z]+" | sort | uniq -c
# expect counts for both "duvo.type=browser" and "duvo.type=http"

# CDP is live on a browser sandbox — talk to Chrome directly
BROWSER=$(docker ps -q --filter label=duvo.type=browser | head -1)
CDP_PORT=$(docker port "$BROWSER" 9222 | head -1 | cut -d: -f2)
curl -s http://localhost:$CDP_PORT/json/version | python3 -m json.tool
# expect: Browser=Chrome/<ver>, Protocol-Version=1.3, webSocketDebuggerUrl=...
```

---

### Step 4 — Observability

**What was built:** `viewer.py` — FastAPI service on `:8000` exposing a JSON API and an HTML dashboard.

**State strategy:** **Docker is the source of truth for liveness**, Redis holds enrichment. Viewer lists containers via label filter on every request, then joins with `sandbox:<jobId>` Redis hashes for extra fields. Result: the view always reflects reality, even if the consumer crashed mid-run.

**Endpoints:**

| Path | Purpose |
|------|---------|
| `GET /` | HTML dashboard, auto-refresh every 2s |
| `GET /healthz` | Redis + Docker reachability check |
| `GET /sandboxes` | Live list with merged Docker + Redis fields |
| `GET /sandboxes/{jobId}` | One sandbox (404 if gone) |
| `DELETE /sandboxes/{jobId}` | Explicit release (step 5) |
| `GET /stats` | Totals, by-type counts, DLQ count |
| `GET /dead` | DLQ entries (step 5) |
| `GET /docs` | Swagger UI (FastAPI default) |

**Verify:**

```bash
# health + summary
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/stats | python3 -m json.tool

# full record — confirm Docker and Redis data are merged
JOB=$(docker ps --filter label=duvo.app=duvo --format '{{.Label "duvo.job"}}' | head -1)
curl -s http://localhost:8000/sandboxes/$JOB | python3 -m json.tool
# expect fields from Docker: container_id, status, started_at
#         fields from Redis: job_created_at, spawned_at, expires_at, ttl_seconds

# drift demo — kill a container externally; viewer reflects within 2s
docker rm -f sandbox-$JOB
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/sandboxes/$JOB
# expect: HTTP 404
```

---

### Step 5 — Failure handling & lifecycle

**What was built:** `reaper.py`, a separate process that runs a reconciliation loop every `RECONCILE_INTERVAL` seconds (default 5). It enforces three cleanup mechanisms and one drift guarantee:

1. **Hard TTL** — every sandbox gets an `expires_at` timestamp (spawn time + `SANDBOX_TTL_SECONDS`, default 60). Reaper kills expired sandboxes.
2. **Failure detection** — containers in non-running states (exited, dead) are pushed to a Redis DLQ (`jobs:dead`) and removed.
3. **Explicit release** — `DELETE /sandboxes/{jobId}` on the viewer, or the **release** button in the dashboard.
4. **Drift reconciliation** — orphan Redis keys (no container) deleted; orphan containers (no Redis key) removed.

**Spawn-time failures** (unknown type, image pull failure) are DLQ'd immediately by the consumer.

**Config (env vars):**
- `SANDBOX_TTL_SECONDS` (default `60`)
- `RECONCILE_INTERVAL` (default `5`)

#### Verify — recommended sequence

For faster feedback, restart consumer and reaper with short TTL / interval:

```bash
# Ctrl+C consumer + reaper, then:
SANDBOX_TTL_SECONDS=30 python consumer.py         # terminal A
RECONCILE_INTERVAL=3 python reaper.py             # terminal D
```

Then run each test from a fresh terminal:

**5a — TTL auto-expiry**

```bash
sleep 35   # wait past one TTL cycle

# expected reaper log lines:
#   INFO reaper reconcile: TTL expired job=<id>
#   INFO reaper reconcile summary: {'expired': N, ...}
```

In the dashboard, the TTL column goes yellow (<30s), red (<10s), then the row disappears.

**5b — Explicit release**

```bash
JOB=$(curl -s http://localhost:8000/sandboxes | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['jobId'])")

curl -s -X DELETE http://localhost:8000/sandboxes/$JOB
# expect: {"released":"<jobId>"}

# both container and Redis key gone immediately:
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/sandboxes/$JOB   # 404
docker exec duvo-redis-1 redis-cli EXISTS sandbox:$JOB                                # 0
```

**5c — Runtime failure → DLQ**

```bash
JOB=$(curl -s http://localhost:8000/sandboxes | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['jobId'])")
docker kill sandbox-$JOB      # exits the container but doesn't remove it
sleep 6                       # wait for reaper

# reaper log expected:
#   WARNING reaper reconcile: failed container job=<id> state=exited -> DLQ

curl -s http://localhost:8000/stats | python3 -m json.tool
# -> "dead" count >= 1

curl -s http://localhost:8000/dead | python3 -m json.tool
# latest entry: {jobId, type, error: "container state=exited", ...}
```

**5d — Spawn-time failure → DLQ**

```bash
# inject a job with an unknown type
docker exec duvo-redis-1 redis-cli LPUSH jobs:pending \
  '{"jobId":"bad-1","type":"nonexistent","created_at":"2026-04-20T00:00:00Z"}'
sleep 3

# consumer log expected:
#   WARNING consumer no spec for type=nonexistent job=bad-1

docker exec duvo-redis-1 redis-cli LRANGE jobs:dead 0 -1 | grep bad-1
# -> entry with error="no spec for type=nonexistent"
```

**5e — Drift reconciliation (orphan Redis key)**

```bash
JOB=$(curl -s http://localhost:8000/sandboxes | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['jobId'])")
docker rm -f sandbox-$JOB                                # container gone, Redis key stays
docker exec duvo-redis-1 redis-cli EXISTS sandbox:$JOB   # -> 1 (orphan)

sleep 5                                                  # > RECONCILE_INTERVAL

docker exec duvo-redis-1 redis-cli EXISTS sandbox:$JOB   # -> 0 (cleaned)
# reaper log: INFO reaper reconcile: orphan redis key removed job=<id>
```

**5f — Clean shutdown**

Ctrl+C all four processes. Expected tail logs:

- consumer: `removed N container(s), cleared M state key(s)`
- reaper: `reaper stopped`
- producer: `producer stopped`
- viewer: `INFO:     Application shutdown complete.`

```bash
docker ps --filter label=duvo.app=duvo -q | wc -l                         # -> 0
docker exec duvo-redis-1 redis-cli --scan --pattern 'sandbox:*' | wc -l   # -> 0
```

---

## Design notes & tradeoffs

**Why Redis (not stdlib `multiprocessing.Queue`)?**
Stdlib would have worked for step 1 alone. At step 4, observability requires shared state across the four processes, and at step 5, TTL needs persistent timestamps. Redis gives us the queue, the enrichment store, and the DLQ in one primitive. Swapping it later would have meant rewriting three modules — paying the 5-minute `docker compose` setup up front was strictly cheaper.

**Why Docker-as-truth for observability (not Redis)?**
Redis can drift from Docker (consumer crashes mid-write, external `docker rm`, etc.). If the viewer reads Redis, the view lags or lies. By querying Docker live on every viewer request, the "what's running" answer is always correct. Redis just supplies fields Docker doesn't know about (e.g., `job_created_at`). The reaper takes on the *only* job of fixing drift — separation of concerns.

**Why a separate reaper process?**
Single-responsibility. The consumer shouldn't care about TTL or failures from containers *it* didn't start. The reaper has one loop, one mental model, and can be restarted independently. In a production multi-consumer deployment you would drop `cleanup_owned` from the consumer and let the reaper own all lifecycle cleanup — the consumer's bulk Ctrl+C cleanup is a demo convenience, not production-shape.

**Why Docker random port assignment (not a pool)?**
Docker's `-p <internal>:<ephemeral>` picks an unused host port per container. Zero conflict management. The assigned port is read back from container inspection and logged. A pool-based approach would add complexity for no step-1–5 benefit.

**Extensibility: one line per new sandbox type.**
`sandbox.py` defines a `SandboxSpec` dataclass. Everything type-specific (image, command, internal port) lives in one entry in the `SPECS` dict. Adding a new type (e.g., a `shell` sandbox) means appending to `SPECS` and optionally to `producer.JOB_TYPES` if you want random emission. Zero changes to `consumer.py`, `viewer.py`, or `reaper.py`.

---

## Known gaps & out-of-scope

| Gap | Why it's not there |
|-----|-------------------|
| **Idle timeout** | Step 5 said "TTL, idle timeout, or explicit release" — chose TTL + explicit release. Idle timeout needs sidecar traffic tracking or a reverse proxy; significant scope for a 60-minute exercise. |
| **Retry / at-least-once** | `BRPOP` is destructive; a crashed consumer loses the in-flight job. Spawn-time failures are captured via DLQ; runtime crashes are not. A production version would use ack-based queues (e.g., `jobs:processing` list + heartbeat, or Redis Streams). |
| **Multi-consumer safety** | Cleanup-owned-on-Ctrl+C kills *all* duvo containers, including ones another consumer may own. Fine for single-host demo; prod would strip that and rely on the reaper. |
| **Authn/authz** | Viewer has no auth. Localhost demo only. |
| **Metrics export** | No Prometheus/OpenTelemetry. `/stats` + logs cover step-4 "observability." |
| **First-job cold pull** | First `http` job waits ~10s while `python:3-slim` pulls; first `browser` job waits ~30s for `chromedp/headless-shell`. One-time per host. |

---

## File layout

```
duvo/
├── docker-compose.yml      # redis:7-alpine
├── requirements.txt        # redis, docker, fastapi, uvicorn
├── job_queue.py            # Redis client + enqueue/dequeue helpers
├── sandbox.py              # Docker client, SandboxSpec registry, spawn/stop/list/cleanup
├── producer.py             # Emits jobs every 3s
├── consumer.py             # BRPOP loop, dispatches via spawn(job), writes Redis state, DLQs failures
├── reaper.py               # TTL + failure + drift reconciliation loop
├── viewer.py               # FastAPI + HTML dashboard
└── README.md               # this file
```

~600 lines of Python total.

---

## Troubleshooting

**`DockerException: Error while fetching server API version: ... FileNotFoundError`**
`sandbox.py` probes common Docker socket paths (`/var/run/docker.sock`, `~/.docker/run/docker.sock`, etc.) for Docker Desktop / Colima / OrbStack. If your setup uses a different path, set `DOCKER_HOST=unix:///path/to/docker.sock` before starting consumer/reaper/viewer.

**`pip install` fails with PEP 668**
Use the venv: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.

**Consumer appears "stuck" on a job**
First run pulls the image. `http` jobs block ~10s for `python:3-slim`; `browser` jobs block ~30s for `chromedp/headless-shell`. Subsequent jobs are instant.
