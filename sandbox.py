import logging
import os
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound

log = logging.getLogger("sandbox")

LABEL_APP = "duvo.app"
LABEL_JOB = "duvo.job"
LABEL_TYPE = "duvo.type"
APP_VALUE = "duvo"

_SOCKET_CANDIDATES = [
    "/var/run/docker.sock",
    f"{Path.home()}/.docker/run/docker.sock",
    f"{Path.home()}/.colima/default/docker.sock",
    f"{Path.home()}/.orbstack/run/docker.sock",
    f"{Path.home()}/.rd/docker.sock",
]


@dataclass(frozen=True)
class SandboxSpec:
    type: str
    image: str
    command: list[str] | None
    internal_port: str
    scheme: str = "http"


SPECS: dict[str, SandboxSpec] = {
    "http": SandboxSpec(
        type="http",
        image="python:3-slim",
        command=["python", "-m", "http.server", "80"],
        internal_port="80/tcp",
    ),
    "browser": SandboxSpec(
        type="browser",
        image="chromedp/headless-shell:latest",
        command=None,
        internal_port="9222/tcp",
    ),
}


_client: docker.DockerClient | None = None


def get_docker() -> docker.DockerClient:
    global _client
    if _client is not None:
        return _client
    if os.getenv("DOCKER_HOST"):
        _client = docker.from_env()
        return _client
    for path in _SOCKET_CANDIDATES:
        if Path(path).exists():
            _client = docker.DockerClient(base_url=f"unix://{path}")
            log.info("docker socket: %s", path)
            return _client
    raise DockerException(
        f"no docker socket found; tried: {_SOCKET_CANDIDATES}. "
        "Set DOCKER_HOST or start Docker Desktop."
    )


def ensure_image(image: str) -> None:
    client = get_docker()
    try:
        client.images.get(image)
        return
    except ImageNotFound:
        log.info("pulling image %s (first run may take a moment)", image)
        client.images.pull(image)


def spawn(job: dict) -> dict:
    spec = SPECS.get(job.get("type"))
    if spec is None:
        raise ValueError(f"unknown sandbox type: {job.get('type')}")
    client = get_docker()
    ensure_image(spec.image)
    name = f"sandbox-{job['jobId']}"
    container = client.containers.run(
        spec.image,
        command=spec.command,
        name=name,
        detach=True,
        ports={spec.internal_port: None},
        labels={
            LABEL_APP: APP_VALUE,
            LABEL_JOB: job["jobId"],
            LABEL_TYPE: spec.type,
        },
    )
    container.reload()
    port_info = container.ports.get(spec.internal_port) or []
    host_port = port_info[0]["HostPort"] if port_info else None
    url = f"{spec.scheme}://localhost:{host_port}" if host_port else None
    return {
        "container_id": container.id,
        "name": name,
        "type": spec.type,
        "host_port": host_port,
        "url": url,
    }


def cleanup_owned() -> int:
    client = get_docker()
    containers = client.containers.list(
        all=True, filters={"label": f"{LABEL_APP}={APP_VALUE}"}
    )
    removed = 0
    for c in containers:
        try:
            c.remove(force=True)
            removed += 1
        except DockerException as e:
            log.warning("failed to remove %s: %s", c.name, e)
    return removed
