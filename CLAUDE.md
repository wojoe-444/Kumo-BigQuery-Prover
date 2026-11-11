# Repository instructions for Claude

This is the BigQuery-only prover for [KumoSQL](https://github.com/wojoe-444/KumoSQL), built on ideas from [Kumo_SQL_Prover](https://github.com/wojoe-444/Kumo_SQL_Prover) without its multi-dialect generality. Practical benefit for KumoSQL comes first: change reports, refactor and dedup proofs, narrower impact.

- **BigQuery only, no DuckDB anywhere.** Not as a dependency, an oracle, or a test helper (`tests/test_no_duckdb.py`). Queries execute on the BigQuery emulator ([docs/emulator-decision.md](docs/emulator-decision.md)). Do not add dialect translation.
- **Unknown is better than wrong.** Every result says "0 wrong" only when it is true. The oracle only refutes; agreeing on built data is `UNKNOWN`. A false proof is a bug.
- **No CI through GitHub.** The check is local: `python -m pytest` (offline, fast) and `python -m pytest -m emulator` (needs the emulator binary, see `tools/install_emulator.sh`; fails, never skips, when it is missing). Tests must be safe to run in parallel.
- **Public repository.** Synthetic examples only: no test-user data, names, counts or quotes, no secrets, real project identifiers or machine-specific paths.
- Keep the held-out split of any corpus untouched; record exposure as "tuned on test".
- A score is moved by editing its file in `benchmarks/results/` and its page in `docs/evals/` in the same pull request.
- Update `README.md` and `docs/` in the pull request that changes behavior.
