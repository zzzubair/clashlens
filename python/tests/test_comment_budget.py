"""No Python file may gain comments; files that lose some must lower their budget.

Run with CLASHLENS_UPDATE_COMMENT_BUDGET=1 to rewrite comment_budget.json after
deleting comments. Directives such as noqa, type: ignore and pragma do not count.
"""

import io
import json
import os
import re
import tokenize
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PYTHON_ROOT.parent
BUDGET_PATH = Path(__file__).with_name("comment_budget.json")
SCANNED = (PYTHON_ROOT / "src", PYTHON_ROOT / "tests", REPOSITORY_ROOT / "development")
DIRECTIVE = re.compile(
    r"#(!|\s*(noqa\b|type:\s*ignore\b|pragma:|fmt:\s*(on|off|skip)\b|-\*- coding))"
)


def comment_count(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    return sum(
        1
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and not DIRECTIVE.match(token.string)
    )


def current_counts() -> dict[str, int]:
    counts = {}
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            count = comment_count(path)
            if count:
                counts[path.relative_to(REPOSITORY_ROOT).as_posix()] = count
    return counts


def test_python_comments_only_go_down() -> None:
    counts = current_counts()
    if os.environ.get("CLASHLENS_UPDATE_COMMENT_BUDGET") == "1":
        budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
        grown = {f: n for f, n in counts.items() if n > budget.get(f, 0)}
        assert not grown, f"Comments grew, delete them instead: {grown}"
        BUDGET_PATH.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
        return
    budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    grown = {
        f: (budget.get(f, 0), n) for f, n in counts.items() if n > budget.get(f, 0)
    }
    shrunk = {
        f: (n, counts.get(f, 0)) for f, n in budget.items() if counts.get(f, 0) < n
    }
    assert not grown, (
        "New comments are not allowed. Make the code say it, or put product "
        f"rules in docs/domain.md. (budget, now): {grown}"
    )
    assert not shrunk, (
        "Comments were removed. Lower the budget with "
        "CLASHLENS_UPDATE_COMMENT_BUDGET=1 uv run pytest tests/test_comment_budget.py. "
        f"(budget, now): {shrunk}"
    )
