"""A pytest plugin that records every pair a test hands to the provers.

    KUMO_RECORD_OUT=pairs.jsonl KUMO_RECORD_PACKAGE=kumo_bq_prover.ported \\
        python -m pytest -p record_prover_calls tests/ported

``KUMO_RECORD_PACKAGE`` is the package that holds ``equivalence``, ``algebraic_equivalence`` and ``smt_equivalence``
(the upstream package ``kumo_sql_prover`` works too, which is how the pairs of tests that were not ported are
reached: such a test fails at its engine call, but it has already called the prover). Only the outermost call is
recorded, and only calls in the BigQuery dialect. One JSON object per line: the two queries, the schema and types
the call was given, whether it passed constraints (keys, NOT NULL, foreign keys), the prover that was called and
its status. Safe under pytest-xdist: each line is one O_APPEND write.
"""

from __future__ import annotations

import functools
import importlib
import json
import os
import threading

_DEPTH = threading.local()
_PROVERS = (
    ("equivalence", "prove_equivalent"),
    ("algebraic_equivalence", "prove_equivalent_algebraic"),
    ("smt_equivalence", "prove_equivalent_smt"),
)


def _write(record: dict) -> None:
    path = os.environ["KUMO_RECORD_OUT"]
    line = (json.dumps(record, default=str) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _status(result) -> str:
    status = getattr(result, "status", result)
    return str(getattr(status, "value", status))


def _wrap(prover: str, function):
    @functools.wraps(function)
    def recorded(left_sql, right_sql, *args, **kwargs):
        outer = not getattr(_DEPTH, "inside", False)
        _DEPTH.inside = True
        try:
            result = function(left_sql, right_sql, *args, **kwargs)
        finally:
            if outer:
                _DEPTH.inside = False
        if outer and kwargs.get("dialect", "bigquery") == "bigquery" and isinstance(left_sql, str) and isinstance(right_sql, str):
            _write({
                "prover": prover, "left": left_sql, "right": right_sql, "status": _status(result),
                "schema": kwargs.get("schema"), "types": kwargs.get("types"),
                "constraints": bool(kwargs.get("constraints")),
            })
        return result

    return recorded


def pytest_configure(config):
    package = os.environ.get("KUMO_RECORD_PACKAGE", "kumo_bq_prover.ported")
    root = importlib.import_module(package)
    for module_name, name in _PROVERS:
        module = importlib.import_module(f"{package}.{module_name}")
        wrapped = _wrap(module_name, getattr(module, name))
        setattr(module, name, wrapped)
        if getattr(root, name, None) is not None:
            setattr(root, name, wrapped)
