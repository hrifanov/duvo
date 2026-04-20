import json
import logging
from datetime import datetime, timedelta, timezone

from job_queue import dequeue, get_client
from sandbox import SANDBOX_TTL_SECONDS, SPECS, cleanup_owned, get_docker, spawn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("consumer")

STATE_KEY_PREFIX = "sandbox:"
DLQ_KEY = "jobs:dead"


def write_state(redis_client, job: dict, info: dict) -> None:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=SANDBOX_TTL_SECONDS)
    key = f"{STATE_KEY_PREFIX}{job['jobId']}"
    redis_client.hset(
        key,
        mapping={
            "jobId": job["jobId"],
            "type": info["type"],
            "name": info["name"],
            "container_id": info["container_id"],
            "host_port": str(info["host_port"] or ""),
            "url": info["url"] or "",
            "job_created_at": job.get("created_at", ""),
            "spawned_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "ttl_seconds": str(SANDBOX_TTL_SECONDS),
            "status": "running",
        },
    )


def send_to_dlq(redis_client, job: dict, error: str) -> None:
    entry = {
        "jobId": job.get("jobId"),
        "type": job.get("type"),
        "error": error,
        "original_job": job,
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    redis_client.lpush(DLQ_KEY, json.dumps(entry))


def clear_state(redis_client) -> int:
    keys = list(redis_client.scan_iter(match=f"{STATE_KEY_PREFIX}*", count=500))
    if not keys:
        return 0
    return redis_client.delete(*keys)


def dispatch(redis_client, job: dict) -> None:
    job_type = job.get("type")
    if job_type not in SPECS:
        log.warning("no spec for type=%s job=%s", job_type, job.get("jobId"))
        send_to_dlq(redis_client, job, f"no spec for type={job_type}")
        return
    try:
        info = spawn(job)
        write_state(redis_client, job, info)
        log.info(
            "spawned %s sandbox job=%s name=%s url=%s ttl=%ss",
            info["type"],
            job["jobId"],
            info["name"],
            info["url"],
            SANDBOX_TTL_SECONDS,
        )
    except Exception as e:
        log.exception("spawn failed for job=%s type=%s: %s", job.get("jobId"), job_type, e)
        send_to_dlq(redis_client, job, f"spawn failed: {e}")


def main() -> None:
    client = get_client()
    client.ping()
    get_docker().ping()
    log.info("consumer started, types=%s, waiting on jobs:pending", list(SPECS))
    try:
        while True:
            job = dequeue(client, timeout=5)
            if job is None:
                continue
            dispatch(client, job)
    except KeyboardInterrupt:
        log.info("consumer stopping, cleaning up owned containers")
    finally:
        removed = cleanup_owned()
        cleared = clear_state(client)
        log.info("removed %d container(s), cleared %d state key(s)", removed, cleared)
        client.close()


if __name__ == "__main__":
    main()
