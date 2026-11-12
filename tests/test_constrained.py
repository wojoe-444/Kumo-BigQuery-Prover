import json
import pathlib

from kumo_bq_prover import constrained

PAIRS = pathlib.Path(__file__).parent.parent / "benchmarks/pairs"

DDL = """
CREATE TABLE parent (
  id INT64 NOT NULL,
  name STRING,
  PRIMARY KEY (id) NOT ENFORCED
);

CREATE TABLE child (
  cid INT64 NOT NULL,
  pid INT64 NOT NULL,
  other INT64,
  PRIMARY KEY (cid) NOT ENFORCED,
  FOREIGN KEY (pid) REFERENCES parent(id) NOT ENFORCED
);
"""


def rows_in(statements, table):
    """The INSERT rows of a table as text, to count them without a database."""
    return [s for s in statements if s.startswith(f"INSERT INTO `{table}` ")]


def test_parse_ddl_reads_keys_and_not_null():
    tables = constrained.parse_ddl(DDL)
    assert tables["parent"].primary_key == ["id"]
    assert tables["child"].foreign_keys == [(["pid"], "parent", ["id"])]
    assert tables["child"].not_null == {"cid", "pid"}
    assert tables["child"].columns["other"] == "INT64"


def test_every_shipped_schema_parses():
    for path in sorted((PAIRS / "schemas").glob("*.sql")):
        tables = constrained.parse_ddl(path.read_text())
        assert tables and all(t.columns and t.primary_key for t in tables.values()), path.name


def test_generated_databases_keep_keys_and_foreign_keys():
    tables = constrained.parse_ddl(DDL)
    names = ["child", "parent"]
    for seed in range(40):
        statements = constrained.database(tables, names, seed)
        parent_ids, child_ids = set(), set()
        for statement in sorted(statements, key=lambda s: not s.startswith("INSERT INTO `parent`")):
            if statement.startswith("INSERT INTO `parent`"):
                body = statement.split("VALUES", 1)[1]
                ids = [int(chunk.strip(" (").split(",")[0]) for chunk in body.split("),")]
                assert len(ids) == len(set(ids)), "duplicate primary key"
                parent_ids = set(ids)
            if statement.startswith("INSERT INTO `child`"):
                body = statement.split("VALUES", 1)[1]
                for chunk in body.split("),"):
                    cell = chunk.strip(" ()").split(",")
                    assert cell[1].strip() != "NULL", "NOT NULL foreign key"
                    assert int(cell[1]) in parent_ids, "dangling foreign key"
                    child_ids.add(int(cell[0]))


def test_dropped_declarations_are_ignored():
    tables = constrained.parse_ddl(DDL)
    drops = constrained.Drops.parse(["fk:child.pid", "pk:parent", "not_null:child.pid"])
    dangling = duplicate = False
    for seed in range(200):
        statements = constrained.database(tables, ["child", "parent"], seed, drops)
        text = "\n".join(statements)
        parents = rows_in(statements, "parent")
        if parents:
            ids = [chunk.strip(" (").split(",")[0] for chunk in parents[0].split("VALUES", 1)[1].split("),")]
            duplicate = duplicate or len(ids) != len(set(ids))
        dangling = dangling or "INSERT INTO `child`" in text
    assert duplicate and dangling


def test_witness_database_completes_columns_and_parents():
    tables = constrained.parse_ddl(DDL)
    statements = constrained.witness_database(tables, ["child", "parent"], {"child": [{"cid": 1, "pid": 7}]})
    text = "\n".join(statements)
    assert "INSERT INTO `child`" in text and "INSERT INTO `parent`" in text
    assert "(7, NULL)" in text  # the parent row the foreign key needs, other columns completed
    without = constrained.witness_database(
        tables, ["child", "parent"], {"child": [{"cid": 1, "pid": 7}]}, constrained.Drops.parse(["fk:child.pid"])
    )
    assert not rows_in(without, "parent")


def test_corpus_labels_and_sizes():
    unsafe = [json.loads(line) for line in (PAIRS / "unsafe-rewrites.jsonl").read_text().splitlines()]
    assert len(unsafe) == 560
    assert {r["label"] for r in unsafe} == {"equivalent", "not_equivalent"}
    sample = [json.loads(line) for line in (PAIRS / "sample-databases.jsonl").read_text().splitlines()]
    assert len(sample) == 286
    assert len({r["id"] for r in sample}) == len(sample)
    assert {r["database"] for r in sample} == {p.stem for p in (PAIRS / "schemas").glob("*.sql")}
    assert {r["label"] for r in sample} == {"equivalent", "not_equivalent"}


CYCLE_DDL = """
CREATE TABLE emp (
  id INT64 NOT NULL,
  boss INT64,
  dept INT64 NOT NULL,
  PRIMARY KEY (id) NOT ENFORCED,
  FOREIGN KEY (boss) REFERENCES emp(id) NOT ENFORCED,
  FOREIGN KEY (dept) REFERENCES dept(id) NOT ENFORCED
);

CREATE TABLE dept (
  id INT64 NOT NULL,
  head INT64 NOT NULL,
  PRIMARY KEY (id) NOT ENFORCED,
  FOREIGN KEY (head) REFERENCES emp(id) NOT ENFORCED
);
"""


def test_self_references_and_cycles_stay_legal():
    tables = constrained.parse_ddl(CYCLE_DDL)
    for seed in range(100):
        rng_rows = {n: [] for n in tables}
        statements = constrained.database(tables, ["emp", "dept"], seed)
        for statement in statements:
            if statement.startswith("INSERT INTO"):
                name = statement.split("`")[1]
                body = statement.split("VALUES", 1)[1]
                rng_rows[name] = [[cell.strip() for cell in chunk.strip(" ();").split(",")] for chunk in body.split("),")]
        ids = {r[0] for r in rng_rows["emp"]}
        dept_ids = {r[0] for r in rng_rows["dept"]}
        for _id, boss, dept in rng_rows["emp"]:
            assert boss == "NULL" or boss in ids
            assert dept in dept_ids
        for _id, head in rng_rows["dept"]:
            assert head in ids
