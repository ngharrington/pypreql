from os.path import dirname, abspath
from typing import Generator

from pytest import fixture
from sqlalchemy.engine import create_engine

from preql import Executor, Dialects, parse, Environment

ENV_PATH = abspath(__file__)


@fixture(scope="session")
def environment():
    yield Environment(working_path=dirname(ENV_PATH))


@fixture(scope="session")
def duckdb_model(environment):
    text = """
key item string;
key value float;
key count int;

metric total_count <- sum(count);


datasource fact_items (
    item:item,
    value:value,
    count:count,
    )
    grain (item)
    address items
;

"""
    environment, statements = parse(text, environment=environment)
    yield environment


@fixture(scope="session")
def duckdb_engine(duckdb_model) -> Generator[Executor, None, None]:
    engine = create_engine("duckdb:///:memory:")

    engine.execute(
        "CREATE TABLE items(item VARCHAR, value DECIMAL(10,2), count INTEGER)"
    )
    # insert two items into the table
    engine.execute("INSERT INTO items VALUES ('jeans', 20.0, 1), ('hammer', 42.2, 2)")
    # validate connection
    results = engine.execute("select 1").fetchall()
    executor = Executor(
        dialect=Dialects.DUCK_DB, engine=engine, environment=duckdb_model
    )
    yield executor


@fixture(scope="session")
def expected_results():
    yield {"total_count": 3}
