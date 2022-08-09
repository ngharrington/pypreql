# from preql.compiler import compile
from os.path import dirname, join

from preql.dialect.sql_server import SqlServerDialect
from preql.parser import parse
from preql.core.models import Select, Grain, QueryDatasource, CTE
from preql.core.query_processor import get_datasource_by_concept_and_grain, datasource_to_ctes, \
    get_query_datasources, process_query
from preql.core.env_processor import generate_graph

def test_finance_queries(adventureworks_engine, environment):
    with open(join(dirname(__file__), 'finance_queries.preql'), 'r', encoding='utf-8') as f:
        file = f.read()
    generator = SqlServerDialect()
    environment, statements = parse(file, environment=environment)
    sql = generator.generate_queries(environment, statements)

    for statement in sql:
        sql = generator.compile_statement(statement)
        results = adventureworks_engine.execute_query(statement)

def test_query_datasources(adventureworks_engine, environment):
    with open(join(dirname(__file__), 'online_sales_queries.preql'), 'r', encoding='utf-8') as f:
        file = f.read()
    generator = SqlServerDialect()
    environment, statements = parse(file, environment=environment)
    assert str(environment.datasources['internet_sales.fact_internet_sales'].grain) == 'Grain<internet_sales.order_line_number,internet_sales.order_number>'

    test:Select = statements[-1] # multipart join

    environment_graph = generate_graph(environment)
    # concepts, datasources = get_query_datasources(environment=environment, graph=environment_graph,
    #                                               statement = test)

    for concept in test.output_components:
        print('-----')
        print('looking at concept')
        print(concept)
        datasource = get_datasource_by_concept_and_grain(
            concept, test.grain, environment, environment_graph
        )

        if concept.name == 'customer_id':
            assert datasource.identifier == 'customers<customer_id>'
        elif concept.address == 'sales_territory.key':
            assert datasource.identifier == 'sales_territories<key>'
        elif concept.name == 'order_number':
            assert datasource.identifier == 'fact_internet_sales<order_line_number,order_number>'
        elif concept.name == 'order_line_number':
            assert datasource.identifier == 'fact_internet_sales<order_line_number,order_number>'
        else:
            raise ValueError(concept)
    # assert set([datasource.identifier for datasource in datasources.values()]) == {'customers<customer_id>',
    #                                                                                'sales_territories<key>',
    #                                                                                }

    joined_datasource:QueryDatasource = [ds for ds in datasources.values() if ds.identifier =='products_join_revenue<category_id>' ][0]
    assert set([c.name for c in joined_datasource.input_concepts]) == {'product_id','category_id', 'revenue'}
    assert set([c.name for c in joined_datasource.output_concepts]) == {'total_revenue','category_id'}

    ctes = []
    for datasource in datasources.values():
        ctes += datasource_to_ctes(datasource)


    assert len(ctes) == 4

    join_ctes = [cte for cte in ctes if cte.name == 'cte_products_join_revenuecategory_id_4899990339111153']
    assert join_ctes
    join_cte:CTE = join_ctes[0]
    assert len(join_cte.source.datasources) == 2
    assert set([c.name for c in join_cte.related_columns]) == {'product_id','category_id', 'revenue'}
    assert set([c.name for c in join_cte.output_columns]) == {'total_revenue','category_id'}


    from preql.dialect.sql_server import render_concept_sql
    for cte in ctes:
        assert len(cte.output_columns)>0
        print(cte.name)
        # if 'default.revenue' in cte.source_map.keys():
        #     assert 'default.total_revenue' not in cte.source_map.keys()
        print(cte.source_map)
        print(cte.output_columns)
        if 'default.revenue' in cte.source_map.keys() and 'revenue' not in cte.name:
            raise ValueError


def test_online_sales_queries(adventureworks_engine, environment):
    with open(join(dirname(__file__), 'online_sales_queries.preql'), 'r', encoding='utf-8') as f:
        file = f.read()
    generator = SqlServerDialect()
    environment, statements = parse(file, environment=environment)
    sql = generator.generate_queries(environment, statements)

    for statement in sql:
        sql = generator.compile_statement(statement)
        results = adventureworks_engine.execute_query(statement)

