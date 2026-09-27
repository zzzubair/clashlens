---
name: clashlens-python-checks
description: Use when writing, reviewing or running tests, or running Python commands and checks in Clash Lens.
user-invocable: false
metadata:
  internal: true
---

# Python commands and checks

## Test behavior

- Test what a user or I would notice if it broke. Don't test spelling of a string, order of function calls, or the shape a private helper returns. If an existing test like that blocks you, tell me and propose deleting it.
- Never edit a test to make it match the code. Either the code is wrong or the test protects nothing.

## Python environment

Before invoking Python, check what is available on the host where the command will run. Use `command -v uv` for project commands and `command -v python3` for standalone commands. Do not assume the unqualified `python` command exists.

For application tests and static checks, use the repository's configured dependency environment. Read `python/README.md` and `python/pyproject.toml`; `python/uv.lock` records the locked dependencies. System Python may lack required packages such as `psycopg` and `minio`.

The current setup uses Python 3.12 through uv. From `python/`, follow the README's environment settings and run the relevant existing check, for example:

```sh
uv run --locked --python 3.12 pytest -q tests/<relevant_test_file>.py
uv run --locked --python 3.12 ruff check <relevant_path>
```

Resolve executable and test paths against the command's actual working directory. From `python/`, use `tests/...`, not `python/tests/...`. If the configured environment is unavailable, report the missing prerequisite rather than substituting an interpreter that lacks the project dependencies.
