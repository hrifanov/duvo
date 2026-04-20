import json
import logging
import os
import time
from datetime import datetime, timezone

from job_queue import get_client
from sandbox import APP_VALUE, LABEL_APP, LABEL_JOB, get_docker, list_owned, stop_by_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("reaper")

STATE_KEY_PREFIX = "sandbox:"
DLQ_KEY = "jobs:dead"
RECONCILE_INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "5"))

RUNNING_STATES = {"running", "created", "restarting"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _dlq(redis_client, job_id: str, reason: str, fields: dict) -> None:
    entry = {
        "jobId": job_id,
        "type": fields.get("type"),
        "error": reason,
        "original_job": {
            "jobId": job_id,
            "type": fields.get("type"),
            "created_at": fields.get("job_created_at"),
        },
        "failed_at": _now().isoformat(),
    }
    redis_client.lpush(DLQ_KEY, json.dumps(entry))


def reconcile(redis_client) -> dict:
    expired = 0
    failed = 0
    orphans_redis = 0
    orphans_docker = 0

    containers = list_owned()
    container_by_job = {c.labels.get(LABEL_JOB): c for c in containers if c.labels.get(LABEL_JOB)}

    redis_job_ids = set()
    for key in redis_client.scan_iter(match=f"{STATE_KEY_PREFIX}*", count=500):
        job_id = key.removeprefix(STATE_KEY_PREFIX)
        redis_job_ids.add(job_id)
        fields = redis_client.hgetall(key)
        container = container_by_job.get(job_id)

        if container is None:
            redis_client.delete(key)
            orphans_redis += 1
            log.info("reconcile: orphan redis key removed job=%s", job_id)
            continue

        state = (container.attrs.get("State") or {}).get("Status", "")
        if state not in RUNNING_STATES:
            redis_client.hset(key, "status", "failed")
            _dlq(redis_client, job_id, f"container state={state}", fields)
            if stop_by_job(job_id):
                redis_client.delete(key)
            failed += 1
            log.warning("reconcile: failed container job=%s state=%s -> DLQ", job_id, state)
            continue

        expires_at = _parse_iso(fields.get("expires_at", ""))
        if expires_at and _now() >= expires_at:
            redis_client.hset(key, "status", "expired")
            if stop_by_job(job_id):
                redis_client.delete(key)
            expired += 1
            log.info("reconcile: TTL expired job=%s", job_id)

    for job_id, container in container_by_job.items():
        if job_id in redis_job_ids:
            continue
        try:
            container.remove(force=True)
            orphans_docker += 1
            log.info("reconcile: orphan docker container removed job=%s", job_id)
        except Exception as e:
            log.warning("reconcile: failed to remove orphan container job=%s: %s", job_id, e)

    return {
        "expired": expired,
        "failed": failed,
        "orphan_redis": orphans_redis,
        "orphan_docker": orphans_docker,
    }


def main() -> None:
    redis_client = get_client()
    redis_client.ping()
    get_docker().ping()
    log.info("reaper started, interval=%ss", RECONCILE_INTERVAL)
    try:
        while True:
            summary = reconcile(redis_client)
            if any(summary.values()):
                log.info("reconcile summary: %s", summary)
            time.sleep(RECONCILE_INTERVAL)
    except KeyboardInterrupt:
        log.info("reaper stopped")
    finally:
        redis_client.close()


if __name__ == "__main__":
    main()
