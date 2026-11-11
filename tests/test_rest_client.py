"""The REST client against a fake server: no emulator binary needed."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kumo_bq_prover.emulator import Client, QueryError
from kumo_bq_prover.oracle import compare
from kumo_bq_prover.prove import prove_equivalent
from kumo_bq_prover.verdict import Verdict


class _Handler(BaseHTTPRequestHandler):
    log: list = []
    answers: dict = {}

    def log_message(self, *args):  # silence
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.log.append((self.path, body))
        if self.path.endswith("/datasets"):
            return self._reply({})
        sql = body["query"]
        if sql in self.answers:
            return self._reply(self.answers[sql])
        return self._reply({"error": "bad"}, status=400)

    def do_DELETE(self):
        self.log.append((self.path, None))
        self._reply({})


def _rows(*values):
    return {
        "schema": {"fields": [{"name": "x", "type": "INTEGER", "mode": "NULLABLE"}]},
        "rows": [{"f": [{"v": str(v)}]} for v in values],
    }


@pytest.fixture
def server():
    handler = type("H", (_Handler,), {"log": [], "answers": {}})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield Client(base_url=f"http://127.0.0.1:{httpd.server_port}"), handler
    httpd.shutdown()


def test_query_decodes_rows_and_uses_default_dataset(server):
    client, handler = server
    handler.answers["SELECT 1"] = _rows(1)
    result = client.query("SELECT 1", dataset="d1")
    assert result.rows == [(1,)]
    path, body = handler.log[-1]
    assert body["defaultDataset"]["datasetId"] == "d1" and body["useLegacySql"] is False


def test_rejected_query_raises(server):
    client, _ = server
    with pytest.raises(QueryError):
        client.query("SELECT nope")


def test_scratch_dataset_is_created_and_deleted(server):
    client, handler = server
    with client.scratch_dataset() as name:
        assert name.startswith("k_")
    assert handler.log[0][0].endswith("/datasets")
    assert handler.log[-1][0].split("?")[0].endswith(f"/datasets/{name}")


def test_oracle_refutes_only_on_different_rows(server):
    client, handler = server
    handler.answers.update({"SELECT a": _rows(1, 2), "SELECT b": _rows(2, 1), "SELECT c": _rows(1)})
    agree = compare(client, "SELECT a", "SELECT b")
    assert agree.verdict is Verdict.UNKNOWN  # agreeing on data is never a proof
    differ = compare(client, "SELECT a", "SELECT c")
    assert differ.verdict is Verdict.REFUTED and differ.left_rows == [(1,), (2,)]


def test_emulator_rejection_is_unknown_not_refuted(server):
    client, handler = server
    handler.answers["SELECT a"] = _rows(1)
    result = compare(client, "SELECT a", "SELECT rejected")
    assert result.verdict is Verdict.UNKNOWN and "emulator" in result.reason


def test_identical_queries_are_proven_without_an_emulator():
    assert prove_equivalent("SELECT 1 AS x", "SELECT 1 AS x").verdict is Verdict.PROVEN
    assert prove_equivalent("SELECT 1 AS x", "SELECT 2 AS x").verdict is Verdict.UNKNOWN
