# Roadmap

1. **Skeleton (this PR).** Emulator choice, lifecycle, REST client, result decoding, refutation-only oracle, 14 synthetic BigQuery-shaped pairs. Not yet run live.
2. **First live run.** Run `tools/run_oracle_eval.py`, fix labels or record emulator gaps per construct, write `benchmarks/results/bq-shapes-oracle.json`.
3. **Port the static layers** from Kumo_SQL_Prover one module at a time, leaving out every other-engine path: parsing and canonical forms, then algebraic rules, then SMT. A port counts only when its tests pass here with no extra engine installed.
4. **Practical targets for KumoSQL:** proofs for change reports (output unchanged, with the evidence), refactor and dedup proofs, narrower impact answers.
5. **Corpus growth:** more real BigQuery and Dataform shapes (STRUCT and ARRAY, QUALIFY, incremental models, CTE stacks, MERGE), synthetic only. A held-out split is set aside before any tuning.
