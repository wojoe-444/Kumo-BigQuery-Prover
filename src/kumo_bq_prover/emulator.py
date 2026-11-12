"""Run and talk to the BigQuery emulator (goccy/bigquery-emulator).

The emulator is an external binary, pinned in ``tools/install_emulator.sh``.
It is found through ``KUMO_BQ_EMULATOR_BIN`` or ``PATH``. This module speaks
the REST API with the standard library only, so the prover has no client
dependency. Nothing here calls real BigQuery: the endpoint is always local.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .values import decode_rows

PROJECT = "kumo-prover"
BINARY_NAME = "bigquery-emulator"


class EmulatorUnavailable(RuntimeError):
    """The emulator binary is missing or did not start."""


class QueryError(RuntimeError):
    """The emulator rejected or failed a query. Callers treat this as unknown."""


def find_binary() -> str | None:
    explicit = os.environ.get("KUMO_BQ_EMULATOR_BIN")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    return shutil.which(BINARY_NAME)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class QueryResult:
    schema: dict
    rows: list[tuple]
    #: The REST rows as the emulator sent them (``{"f": [{"v": ...}]}``), for callers that decode by type.
    raw_rows: list = field(default_factory=list)


@dataclass
class Client:
    """A minimal REST client for one emulator endpoint."""

    base_url: str
    project: str = PROJECT
    timeout: float = 60.0
    _opener: urllib.request.OpenerDirector = field(
        default_factory=lambda: urllib.request.build_opener(urllib.request.ProxyHandler({})),
        repr=False,
    )

    def _post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise QueryError(f"HTTP {error.code}: {detail[:500]}") from None
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as error:
            raise EmulatorUnavailable(str(error)) from None

    def _get(self, path: str) -> dict:
        try:
            with self._opener.open(f"{self.base_url}{path}", timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise QueryError(f"HTTP {error.code}: {detail[:500]}") from None
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as error:
            raise EmulatorUnavailable(str(error)) from None

    def create_dataset(self, dataset: str) -> None:
        self._post(
            f"/bigquery/v2/projects/{self.project}/datasets",
            {"datasetReference": {"projectId": self.project, "datasetId": dataset}},
        )

    def delete_dataset(self, dataset: str) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/bigquery/v2/projects/{self.project}/datasets/{dataset}?deleteContents=true",
            method="DELETE",
        )
        try:
            self._opener.open(request, timeout=self.timeout).close()
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            pass  # cleanup is best effort; datasets are unique per run

    def query(
        self, sql: str, dataset: str | None = None, params: list[dict] | None = None, decode: bool = True
    ) -> QueryResult:
        """Run one GoogleSQL statement and return its decoded rows.

        ``params`` are named query parameters in the REST form
        (``{"name": "p", "parameterType": {"type": "INT64"}, "parameterValue": {"value": "1"}}``).
        ``decode=False`` skips the typed decoding (``rows`` stays empty) and leaves the REST rows in
        ``raw_rows`` for callers that decode by type themselves. A result that cannot be decoded
        raises ``QueryError``, so the oracle treats it as unknown.
        """
        body: dict = {"query": sql, "useLegacySql": False, "maxResults": 10000}
        if params:
            body["parameterMode"] = "NAMED"
            body["queryParameters"] = params
        if dataset:
            body["defaultDataset"] = {"projectId": self.project, "datasetId": dataset}
        response = self._post(f"/bigquery/v2/projects/{self.project}/queries", body)
        if response.get("errors"):
            raise QueryError(json.dumps(response["errors"])[:500])
        schema = response.get("schema", {})
        rows = list(response.get("rows", []))
        job = response.get("jobReference", {})
        token = response.get("pageToken")
        while token:
            page = self._get(
                f"/bigquery/v2/projects/{self.project}/queries/{job['jobId']}?pageToken={token}&maxResults=10000"
            )
            rows.extend(page.get("rows", []))
            token = page.get("pageToken")
        if not decode:
            return QueryResult(schema=schema, rows=[], raw_rows=rows)
        try:
            decoded = decode_rows(schema, rows)
        except (KeyError, ValueError, TypeError, ArithmeticError) as error:
            raise QueryError(f"result could not be decoded: {type(error).__name__}: {error}"[:300]) from None
        return QueryResult(schema=schema, rows=decoded, raw_rows=rows)

    @contextmanager
    def scratch_dataset(self) -> Iterator[str]:
        """A dataset that exists only inside the ``with`` block."""
        dataset = "k_" + uuid.uuid4().hex[:12]
        self.create_dataset(dataset)
        try:
            yield dataset
        finally:
            self.delete_dataset(dataset)


def _guard_memory(process: subprocess.Popen, limit_mb: int) -> None:
    status = f"/proc/{process.pid}/status"
    while process.poll() is None:
        try:
            with open(status) as handle:
                for line in handle:
                    if line.startswith("VmRSS:") and int(line.split()[1]) > limit_mb * 1024:
                        process.kill()
                        return
        except OSError:
            return
        time.sleep(0.3)


@contextmanager
def running_emulator(
    binary: str | None = None, startup_timeout: float = 60.0, max_rss_mb: int | None = None
) -> Iterator[Client]:
    """Start a private emulator on a free local port and stop it afterwards.

    ``max_rss_mb`` kills the emulator when it holds more resident memory than that, so one runaway
    query cannot take the machine down; the caller sees the usual ``EmulatorUnavailable``.
    """
    binary = binary or find_binary()
    if not binary:
        raise EmulatorUnavailable(
            f"{BINARY_NAME} not found: run tools/install_emulator.sh or set KUMO_BQ_EMULATOR_BIN"
        )
    port = _free_port()
    process = subprocess.Popen(
        [binary, f"--project={PROJECT}", "--host=127.0.0.1", f"--port={port}", f"--grpc-port={_free_port()}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = Client(base_url=f"http://127.0.0.1:{port}")
    if max_rss_mb:
        threading.Thread(target=_guard_memory, args=(process, max_rss_mb), daemon=True).start()
    try:
        deadline = time.monotonic() + startup_timeout
        while True:
            if process.poll() is not None:
                raise EmulatorUnavailable(f"{BINARY_NAME} exited with status {process.returncode}")
            try:
                client._get(f"/bigquery/v2/projects/{PROJECT}/datasets")
                break
            except (EmulatorUnavailable, QueryError):
                if time.monotonic() > deadline:
                    raise EmulatorUnavailable("emulator did not become ready in time") from None
                time.sleep(0.2)
        yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
