# Base images are pinned by digest; the tag each one was taken from is noted
# above it. Update both together.
# python:3.12-slim
FROM docker.io/library/python@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS dependencies

# uv 0.13.5
COPY --from=ghcr.io/astral-sh/uv@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /uvx /usr/local/bin/
WORKDIR /opt/clashlens
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY python/pyproject.toml python/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# python:3.12-slim
FROM docker.io/library/python@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

RUN groupadd --gid 10001 clashlens \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin clashlens
COPY --from=dependencies /opt/clashlens/.venv /opt/venv
COPY --chown=10001:10001 python/src /opt/clashlens/src
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/clashlens/src PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/clashlens
VOLUME ["/spool"]
USER 10001:10001
ENTRYPOINT ["python", "-m", "clashlens.cli"]
