FROM docker.io/library/python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /usr/local/bin/
WORKDIR /workspace/python
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/workspace:/workspace/python/src

COPY python/pyproject.toml python/uv.lock ./
RUN uv sync --locked --all-groups --no-install-project

COPY python/src ./src
COPY python/tests ./tests
COPY python/testdata ./testdata
COPY .github /workspace/.github
COPY deploy /workspace/deploy
COPY development /workspace/development
COPY docs/data /workspace/docs/data
COPY testdata /workspace/testdata

CMD ["sh", "-c", "uv run ruff check . ../development/*.py && uv run python -m compileall -q src ../development && uv run pytest -q && uv run pytest -q ../development/test_fixtures.py"]
