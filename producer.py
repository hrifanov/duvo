import logging
import random
import time
import uuid
from datetime import datetime, timezone

from job_queue import enqueue, get_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("producer")

INTERVAL_SECONDS = 3
JOB_TYPES = ["http", "browser"]


def make_job() -> dict:
    return {
        "jobId": uuid.uuid4().hex[:8],
        "type": random.choice(JOB_TYPES),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    client = get_client()
    client.ping()
    log.info("producer started, interval=%ss", INTERVAL_SECONDS)
    try:
        while True:
            job = make_job()
            enqueue(client, job)
            log.info("enqueued job %s type=%s", job["jobId"], job["type"])
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info("producer stopped")
    finally:
        client.close()


if __name__ == "__main__":
    main()
