# Roadmap

1. **Skeleton (done).** Emulator choice, lifecycle, REST client, result decoding, refutation-only oracle, 14 synthetic BigQuery-shaped pairs.
2. **First live run (done, 2026-11-11).** 5 of 6 refuted, 0 wrong; two emulator gaps recorded in [evals/bq-shapes.md](evals/bq-shapes.md). Next: the evals in [hillclimb-evals.md](hillclimb-evals.md), starting with numeric traps and documented rewrites.
3. **Port the static layers** from Kumo_SQL_Prover one module at a time, leaving out every other-engine path: parsing and canonical forms, then algebraic rules, then SMT. A port counts only when its tests pass here with no extra engine installed.
4. **Practical targets for KumoSQL:** proofs for change reports (output unchanged, with the evidence), refactor and dedup proofs, narrower impact answers.
5. **Corpus growth:** more real BigQuery and Dataform shapes (STRUCT and ARRAY, QUALIFY, incremental models, CTE stacks, MERGE), synthetic only. A held-out split is set aside before any tuning.
