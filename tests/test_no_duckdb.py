"""This prover never uses DuckDB: not as a dependency, not as an oracle, not in tests."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"\bduckdb\b", re.IGNORECASE)


def test_no_duckdb_in_code_or_dependencies():
    files = [ROOT / "pyproject.toml"]
    files += (ROOT / "src").rglob("*.py")
    files += (ROOT / "tools").glob("*")
    files += [p for p in (ROOT / "tests").glob("*.py") if p.name != pathlib.Path(__file__).name]
    offenders = [str(p.relative_to(ROOT)) for p in files if p.is_file() and PATTERN.search(p.read_text())]
    assert not offenders, offenders
