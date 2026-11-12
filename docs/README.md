# Docs

- [emulator-decision.md](emulator-decision.md): which BigQuery emulator is the oracle, and why.
- [roadmap.md](roadmap.md): the order of work.
- [evals/bq-shapes.md](evals/bq-shapes.md): the first synthetic corpus and its (unmeasured) score.
- [hillclimb-evals.md](hillclimb-evals.md): the ranked list of BigQuery evals to improve this prover against.
- [evals/pairs.md](evals/pairs.md): the two labelled pair corpora, their emulator scores, and what they found.
- [ported-layers.md](ported-layers.md): the static proof layers ported from Kumo_SQL_Prover, what was cut and changed, and how a proof is decided.
- [evals/googlesql-compliance.md](evals/googlesql-compliance.md): Google's GoogleSQL compliance cases run on the emulator, with scores for the development and held-out files.
- [evals/sample-databases.md](evals/sample-databases.md): 286 pairs over six public sample databases, equivalent only under declared keys, with constraint-aware data.
- [evals/unsafe-rewrites.md](evals/unsafe-rewrites.md): 560 pairs of rewrites that look valid (NOT IN with NULLs, COUNT, join duplicates, outer-join predicates), with the labels the run decided.
- [evals/jaffle-shop.md](evals/jaffle-shop.md): 15 refactors of a five-model dbt project, built as pipelines on the emulator.
- [emulator-gaps.md](emulator-gaps.md): where the emulator disagrees with the reference, what the oracle does about it, and the residual risk.
