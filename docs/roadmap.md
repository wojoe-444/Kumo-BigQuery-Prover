# Roadmap

1. **Skeleton (done).** Emulator choice, lifecycle, REST client, result decoding, refutation-only oracle, 14 synthetic BigQuery-shaped pairs.
2. **First live run (done, 2026-11-11).** 5 of 6 refuted, 0 wrong; two emulator gaps recorded in [evals/bq-shapes.md](evals/bq-shapes.md). Next: the evals in [hillclimb-evals.md](hillclimb-evals.md), starting with numeric traps and documented rewrites.
3. **Port the static layers (done, 2026-11-12, [ported-layers.md](ported-layers.md)).** Ported as one block, not module by module: 169 modules, 3,098 ported tests passing. Documented rewrites 18 of 19 proved, numeric traps 51 of 65, 0 false proofs. Then the same day: the removed witness tests replaced by a proof audit over 1,186 upstream pairs ([evals/proof-audit.md](evals/proof-audit.md)) and a frozen held-out set ([evals/heldout.md](evals/heldout.md)). Left: the window-pruning false proof upstream (its own thread), constraint-aware data for the audit (keys, NOT NULL, foreign keys), and measuring layers on pipeline corpora (its own thread). Original wording:

   **Port the static layers** from Kumo_SQL_Prover one module at a time, leaving out every other-engine path: parsing and canonical forms, then algebraic rules, then SMT. A port counts only when its tests pass here with no extra engine installed.
4. **Practical targets for KumoSQL:** proofs for change reports (output unchanged, with the evidence), refactor and dedup proofs, narrower impact answers.
5. **Corpus growth:** more real BigQuery and Dataform shapes (STRUCT and ARRAY, QUALIFY, incremental models, CTE stacks, MERGE), synthetic only. A held-out split is set aside before any tuning.
