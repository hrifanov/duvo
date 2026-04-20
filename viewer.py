"""FastAPI observability service.

Endpoints:
    GET  /healthz             Redis + Docker reachable.
    GET  /sandboxes           List live sandboxes (Docker filtered by label,
                              enriched from Redis).
    GET  /sandboxes/{jobId}   One sandbox.
    DELETE /sandboxes/{jobId} Force release (stop container + drop state).
    GET  /stats               total, by_type, dead (DLQ depth).
    GET  /dead?limit=N        Newest N DLQ entries.
    GET  /                    HTML dashboard, auto-refresh 2s.

Design: Docker is the source of truth for *liveness* (a container either
exists or it doesn't). Redis supplies *enrichment* (spawn time, TTL, jobId
metadata). Listing always starts from Docker, then overlays Redis fields, so
a stale Redis row never invents a phantom sandbox in the UI.

Run: `uvicorn viewer:app --reload --port 8080`.
"""

import json
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from job_queue import get_client
from sandbox import APP_VALUE, LABEL_APP, LABEL_JOB, LABEL_TYPE, get_docker, stop_by_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("viewer")

STATE_KEY_PREFIX = "sandbox:"
DLQ_KEY = "jobs:dead"
app = FastAPI(title="duvo viewer")
_redis = get_client()


def _first_host_port(container) -> str | None:
    for _, bindings in (container.ports or {}).items():
        if bindings:
            return bindings[0].get("HostPort")
    return None


def _enrich(container) -> dict:
    job_id = container.labels.get(LABEL_JOB)
    host_port = _first_host_port(container)
    base = {
        "jobId": job_id,
        "type": container.labels.get(LABEL_TYPE),
        "name": container.name,
        "container_id": container.id[:12],
        "status": container.status,
        "host_port": host_port,
        "url": f"http://localhost:{host_port}" if host_port else None,
        "started_at": container.attrs.get("State", {}).get("StartedAt"),
    }
    if job_id:
        redis_fields = _redis.hgetall(f"{STATE_KEY_PREFIX}{job_id}")
        if redis_fields:
            for k in ("job_created_at", "spawned_at", "expires_at", "ttl_seconds"):
                v = redis_fields.get(k)
                if v:
                    base[k] = v
    return base


def _list_containers(label_filter: str):
    return get_docker().containers.list(filters={"label": label_filter})


@app.get("/healthz")
def healthz():
    try:
        _redis.ping()
        get_docker().ping()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(503, detail=str(e))


@app.get("/sandboxes")
def list_sandboxes():
    return [_enrich(c) for c in _list_containers(f"{LABEL_APP}={APP_VALUE}")]


@app.get("/sandboxes/{job_id}")
def get_sandbox(job_id: str):
    containers = _list_containers(f"{LABEL_JOB}={job_id}")
    if not containers:
        raise HTTPException(404, detail=f"no sandbox with jobId={job_id}")
    return _enrich(containers[0])


@app.delete("/sandboxes/{job_id}")
def release_sandbox(job_id: str):
    removed = stop_by_job(job_id)
    if not removed:
        raise HTTPException(404, detail=f"no sandbox with jobId={job_id}")
    _redis.delete(f"{STATE_KEY_PREFIX}{job_id}")
    return {"released": job_id}


@app.get("/stats")
def stats():
    containers = _list_containers(f"{LABEL_APP}={APP_VALUE}")
    by_type: dict[str, int] = {}
    for c in containers:
        t = c.labels.get(LABEL_TYPE) or "unknown"
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "total": len(containers),
        "by_type": by_type,
        "dead": _redis.llen(DLQ_KEY),
    }


@app.get("/dead")
def dead(limit: int = Query(20, ge=1, le=200)):
    raw = _redis.lrange(DLQ_KEY, 0, limit - 1)
    return [json.loads(r) for r in raw]


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>duvo sandboxes</title>
<style>
body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; padding: 20px; background: #111; color: #eee; }
h1 { margin: 0 0 6px 0; font-weight: 600; }
.sub { color: #7a7; margin-bottom: 18px; font-size: 13px; }
.stats { margin-bottom: 14px; color: #9a9; }
.stats span { margin-right: 16px; }
.stats .dead { color: #f66; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 7px 12px; border-bottom: 1px solid #222; }
th { color: #6cf; font-weight: 600; }
tr:hover td { background: #181818; }
a { color: #9cf; text-decoration: none; }
a:hover { text-decoration: underline; }
button.release {
  background: #311; color: #f99; border: 1px solid #633; padding: 3px 9px;
  border-radius: 3px; cursor: pointer; font: inherit; font-size: 12px;
}
button.release:hover { background: #522; color: #fcc; }
.type-http { color: #6e6; }
.type-browser { color: #f96; }
.ttl-warn { color: #fc6; }
.ttl-critical { color: #f66; }
.empty { color: #666; padding: 20px 0; }
</style>
</head>
<body>
<h1>duvo sandboxes</h1>
<div class="sub">auto-refresh every 2s &middot; docker + redis &middot; TTL-managed</div>
<div class="stats" id="stats">loading…</div>
<table>
<thead><tr>
<th>name</th><th>type</th><th>jobId</th><th>status</th><th>url</th><th>started</th><th>TTL</th><th></th>
</tr></thead>
<tbody id="rows"></tbody>
</table>
<div id="empty" class="empty" style="display:none">no sandboxes running</div>
<script>
function ttlCell(expiresAt) {
  if (!expiresAt) return '';
  const remaining = Math.floor((new Date(expiresAt) - new Date()) / 1000);
  if (remaining < 0) return `<span class="ttl-critical">expired</span>`;
  const cls = remaining < 10 ? 'ttl-critical' : remaining < 30 ? 'ttl-warn' : '';
  return `<span class="${cls}">${remaining}s</span>`;
}
async function releaseSandbox(jobId) {
  if (!confirm(`Release sandbox ${jobId}?`)) return;
  const r = await fetch(`/sandboxes/${jobId}`, { method: 'DELETE' });
  if (!r.ok) alert('release failed: ' + r.status);
  refresh();
}
async function refresh() {
  try {
    const [list, stats] = await Promise.all([
      fetch('/sandboxes').then(r => r.json()),
      fetch('/stats').then(r => r.json()),
    ]);
    const typeStr = Object.entries(stats.by_type).map(([k,v])=>`<span>${k}: ${v}</span>`).join('');
    const deadStr = stats.dead ? `<span class="dead">dead: ${stats.dead}</span>` : '';
    document.getElementById('stats').innerHTML = `<span>total: ${stats.total}</span>${typeStr}${deadStr}`;
    const body = document.getElementById('rows');
    const empty = document.getElementById('empty');
    empty.style.display = list.length === 0 ? 'block' : 'none';
    body.innerHTML = list.map(s => `
      <tr>
        <td>${s.name}</td>
        <td class="type-${s.type}">${s.type || '?'}</td>
        <td>${s.jobId || ''}</td>
        <td>${s.status || ''}</td>
        <td>${s.url ? `<a href="${s.url}" target="_blank">${s.url}</a>` : ''}</td>
        <td>${(s.started_at || '').slice(11,19)}</td>
        <td>${ttlCell(s.expires_at)}</td>
        <td><button class="release" onclick="releaseSandbox('${s.jobId}')">release</button></td>
      </tr>`).join('');
  } catch (e) {
    document.getElementById('stats').textContent = 'error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _DASHBOARD_HTML
