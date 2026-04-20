import logging

from job_queue import dequeue, get_client
from sandbox import SPECS, cleanup_owned, get_docker, spawn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("consumer")


def dispatch(job: dict) -> None:
    job_type = job.get("type")
    if job_type not in SPECS:
        log.warning("no spec for type=%s job=%s", job_type, job.get("jobId"))
        return
    try:
        info = spawn(job)
        log.info(
            "spawned %s sandbox job=%s name=%s url=%s",
            info["type"],
            job["jobId"],
            info["name"],
            info["url"],
        )
    except Exception as e:
        log.exception("spawn failed for job=%s type=%s: %s", job.get("jobId"), job_type, e)


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
            dispatch(job)
    except KeyboardInterrupt:
        log.info("consumer stopping, cleaning up owned containers")
    finally:
        removed = cleanup_owned()
        log.info("removed %d owned container(s)", removed)
        client.close()


if __name__ == "__main__":
    main()
