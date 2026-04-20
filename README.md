# duvo — sandbox orchestration

Building a lightweight sandbox orchestration platform across 5 steps. Each step extends the previous one.

## Setup

```bash
docker compose up -d redis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

All subsequent `python` commands below assume the venv is active. Deactivate with `deactivate` when done.

## Step 1 — Job queue (producer + consumer) ✅

Producer enqueues jobs to Redis, consumer pops and logs them.

**Run:**
```bash
# terminal A
python consumer.py

# terminal B
python producer.py
```

**Verify:**
```bash
# producer logs: "enqueued job <id> type=http" every 3s
# consumer logs: "handling http job <id> created_at=..."

# queue drains (should be 0 while consumer runs):
docker exec duvo-redis-1 redis-cli LLEN jobs:pending

# inject a job manually:
docker exec duvo-redis-1 redis-cli LPUSH jobs:pending '{"jobId":"manual1","type":"http","created_at":"2026-04-20T00:00:00Z"}'

# stop consumer, enqueue a few, start consumer — it drains the backlog:
docker exec duvo-redis-1 redis-cli LLEN jobs:pending   # grows
python consumer.py                                     # LLEN returns to 0
```

Clean exit on Ctrl+C (no tracebacks).

---

## Step 2 — Run a container per job ✅

Consumer spawns a `python:3-slim` container running `python -m http.server 80` per `http` job. Docker assigns a random host port; consumer logs `http://localhost:<port>`. On Ctrl+C, consumer removes containers it owns (label `duvo.app=duvo`).

**Requires:** Docker daemon running. First job pulls `python:3-slim` (~45MB, one-time).

**Run:** same as step 1 (`python consumer.py` + `python producer.py`).

**Verify:**
```bash
# watch owned sandboxes appear:
docker ps --filter label=duvo.app=duvo

# curl a logged URL (consumer logs look like "url=http://localhost:54321"):
curl http://localhost:<port>/                          # directory listing HTML

# inspect labels:
docker inspect --format '{{json .Config.Labels}}' sandbox-<jobId>

# stop consumer with Ctrl+C -> cleanup logs "removed N owned container(s)":
docker ps --filter label=duvo.app=duvo                 # empty

# if consumer crashed without cleanup, sweep manually:
docker rm -f $(docker ps -aq --filter label=duvo.app=duvo)
```

---

## Step 3 — Multiple sandbox types ✅

Added `browser` type using `chromedp/headless-shell:latest` with CDP on container port 9222. Adding a new type = one entry in `SPECS` (see `sandbox.py`). No changes to consumer dispatch; `spawn(job)` is generic over `SandboxSpec`.

**Requires:** first `browser` job pulls `chromedp/headless-shell` (~130MB, one-time).

**Run:** same as before. Producer now emits random `http` or `browser` jobs.

**Verify:**
```bash
# mix of sandbox types running:
docker ps --filter label=duvo.app=duvo --format "table {{.Names}}\t{{.Labels}}\t{{.Ports}}"

# CDP version endpoint on a browser sandbox (find port via "docker port"):
BROWSER=$(docker ps -q --filter label=duvo.type=browser | head -1)
PORT=$(docker port "$BROWSER" 9222 | head -1 | cut -d: -f2)
curl -s http://localhost:$PORT/json/version    # Browser, Chrome UA, webSocketDebuggerUrl

# list open tabs (confirms CDP is live):
curl -s http://localhost:$PORT/json

# http sandbox still works:
HTTP=$(docker ps -q --filter label=duvo.type=http | head -1)
HPORT=$(docker port "$HTTP" 80 | head -1 | cut -d: -f2)
curl -s http://localhost:$HPORT/ | head -3

# inject a specific type manually:
docker exec duvo-redis-1 redis-cli LPUSH jobs:pending \
  '{"jobId":"manual-browser","type":"browser","created_at":"2026-04-20T00:00:00Z"}'
```

**Adding another type (demo of extension point):**
Append to `SPECS` in `sandbox.py`:
```python
"shell": SandboxSpec(type="shell", image="alpine:latest",
                     command=["sh","-c","nc -l -p 8080"], internal_port="8080/tcp"),
```
Then `"shell"` is already accepted by producer (add to `JOB_TYPES`) and consumer.

---

## Step 4 — Observable orchestration (todo)

Small HTTP view (FastAPI) that derives current system state from Redis at any time. Consumer writes `sandboxes:<id>` hashes + `sandboxes:active` set as it spawns/reaps.

**Planned verify:**
```bash
curl http://localhost:8000/sandboxes                   # list of active
curl http://localhost:8000/sandboxes/<id>              # one sandbox state

# ground truth in Redis:
docker exec duvo-redis-1 redis-cli SMEMBERS sandboxes:active
docker exec duvo-redis-1 redis-cli HGETALL sandboxes:<id>
```

State under `/sandboxes` should equal Redis truth.

---

## Step 5 — Failure + lifecycle (todo)

TTL / idle timeout / explicit release. Reaper loop (or keyspace notifications) kills containers past TTL. Failed spawns go to a DLQ.

**Planned verify:**
```bash
# spawn a job with short TTL, wait past it:
# container should disappear automatically
docker ps                                              # gone after TTL
docker exec duvo-redis-1 redis-cli SMEMBERS sandboxes:active   # cleaned

# kill a sandbox externally, observe reconciliation:
docker kill <sandbox-id>
curl http://localhost:8000/sandboxes/<id>              # state = failed

# DLQ receives unhandleable jobs:
docker exec duvo-redis-1 redis-cli LRANGE jobs:dead 0 -1
```

---

## Layout

- `docker-compose.yml` — Redis 7
- `job_queue.py` — Redis client + `enqueue` / `dequeue` on `jobs:pending`
- `producer.py` — emits `{jobId, type, created_at}` every 3s
- `consumer.py` — `BRPOP` loop, dispatches by `type` via `HANDLERS`

## Config

- `REDIS_HOST` (default `localhost`)
- `REDIS_PORT` (default `6379`)
