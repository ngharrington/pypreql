from typing import List, Union, Optional, Dict

from jinja2 import Template

from preql.core.enums import FunctionType, WindowType, PurposeLineage, JoinType
from preql.core.hooks import BaseProcessingHook
from preql.core.enums import Purpose, DataType
from preql.core.models import (
    Concept,
    CTE,
    ProcessedQuery,
    CompiledCTE,
    Conditional,
    Expr,
    Comparison,
    Function,
    OrderItem,
    WindowItem,
)
from preql.core.models import Environment, Select
from preql.core.query_processor import process_query
from preql.dialect.common import render_join
from preql.utility import unique

INVALID_REFERENCE_STRING = "INVALID_REFERENCE_BUG"

WINDOW_FUNCTION_MAP = {
    WindowType.ROW_NUMBER: lambda window, sort, order: f"row_number() over ( order by {sort} {order})"
}

DATATYPE_MAP = {
    DataType.STRING: "string",
    DataType.INTEGER: "int",
    DataType.FLOAT: "float",
    DataType.BOOL: "bool",
}

FUNCTION_MAP = {
    # generic types
    FunctionType.CAST: lambda x: f"cast({x[0]} as {x[1]})",
    # aggregate types
    FunctionType.COUNT_DISTINCT: lambda x: f"count(distinct {x[0]})",
    FunctionType.COUNT: lambda x: f"count({x[0]})",
    FunctionType.SUM: lambda x: f"sum({x[0]})",
    FunctionType.LENGTH: lambda x: f"length({x[0]})",
    FunctionType.AVG: lambda x: f"avg({x[0]})",
    FunctionType.MAX: lambda x: f"max({x[0]})",
    FunctionType.MIN: lambda x: f"min({x[0]})",
    FunctionType.LIKE: lambda x: f" CASE WHEN {x[0]} like {x[1]} THEN 1 ELSE 0 END",
    FunctionType.NOT_LIKE: lambda x: f" CASE WHEN {x[0]} like {x[1]} THEN 0 ELSE 1 END",
    # date types
    FunctionType.DATE: lambda x: f"date({x[0]})",
    FunctionType.DATETIME: lambda x: f"datetime({x[0]})",
    FunctionType.TIMESTAMP: lambda x: f"timestamp({x[0]})",
    FunctionType.SECOND: lambda x: f"second({x[0]})",
    FunctionType.MINUTE: lambda x: f"minute({x[0]})",
    FunctionType.HOUR: lambda x: f"hour({x[0]})",
    FunctionType.DAY: lambda x: f"day({x[0]})",
    FunctionType.MONTH: lambda x: f"month({x[0]})",
    FunctionType.YEAR: lambda x: f"year({x[0]})",
    # string types
    FunctionType.CONCAT: lambda x: f"concat({','.join(x)})",
}

FUNCTION_GRAIN_MATCH_MAP = {
    **FUNCTION_MAP,
    FunctionType.COUNT_DISTINCT: lambda args: f"{args[0]}",
    FunctionType.COUNT: lambda args: f"{args[0]}",
    FunctionType.SUM: lambda args: f"{args[0]}",
    FunctionType.AVG: lambda args: f"{args[0]}",
    FunctionType.MAX: lambda args: f"{args[0]}",
    FunctionType.MIN: lambda args: f"{args[0]}",
}


GENERIC_SQL_TEMPLATE = Template(
    """{%- if ctes %}
WITH {% for cte in ctes %}
{{cte.name}} as ({{cte.statement}}){% if not loop.last %},{% endif %}{% endfor %}{% endif %}
SELECT
{%- if limit is not none %}
TOP {{ limit }}{% endif %}
{%- for select in select_columns %}
    {{ select }}{% if not loop.last %},{% endif %}{% endfor %}
FROM
    {{ base }}{% if joins %}{% for join in joins %}
{{ join }}{% endfor %}{% endif %}
{% if where %}WHERE
    {{ where }}
{% endif %}
{%- if group_by %}GROUP BY {% for group in group_by %}
    {{group}}{% if not loop.last %},{% endif %}{% endfor %}{% endif %}
{%- if order_by %}
ORDER BY {% for order in order_by %}
    {{ order }}{% if not loop.last %},{% endif %}
{% endfor %}{% endif %}
"""
)


def check_lineage(c: Concept, cte: CTE) -> bool:
    checks = []
    if not c.lineage:
        return True
    for sub_c in c.lineage.arguments:
        if not isinstance(sub_c, Concept):
            continue
        if sub_c.address in cte.source_map or (
            sub_c.lineage and check_lineage(sub_c, cte)
        ):
            checks.append(True)
        else:
            checks.append(False)
    return all(checks)


def safe_quote(string: str, quote_char: str):
    # split dotted identifiers
    # TODO: evaluate if we need smarter parsing for strings that could actually include .
    components = string.split(".")
    return ".".join([f"{quote_char}{string}{quote_char}" for string in components])


class BaseDialect:
    WINDOW_FUNCTION_MAP = WINDOW_FUNCTION_MAP
    FUNCTION_MAP = FUNCTION_MAP
    FUNCTION_GRAIN_MATCH_MAP = FUNCTION_GRAIN_MATCH_MAP
    QUOTE_CHARACTER = "`"
    SQL_TEMPLATE = GENERIC_SQL_TEMPLATE
    DATATYPE_MAP = DATATYPE_MAP

    def render_order_item(self, order_item: OrderItem, ctes: List[CTE]) -> str:
        matched_ctes = [
            cte
            for cte in ctes
            if order_item.expr.address in [a.address for a in cte.output_columns]
        ]
        if not matched_ctes:
            raise ValueError(f"No source found for concept {order_item.expr}")
        selected = matched_ctes[0]
        return (
            f"{selected.name}.{order_item.expr.safe_address} {order_item.order.value}"
        )

    def render_literal(self, v) -> str:
        if isinstance(v, str):
            return f"'{v}'"
        elif isinstance(v, DataType):
            return DATATYPE_MAP.get(v, "UNMAPPEDDTYPE")
        else:
            return v

    def render_concept_sql(self, c: Concept, cte: CTE, alias: bool = True) -> str:
        # only recurse while it's in sources of the current cte

        if (c.lineage and check_lineage(c, cte)) and not cte.source_map.get(
            c.address, ""
        ).startswith("cte"):
            if isinstance(c.lineage, WindowItem):
                # args = [render_concept_sql(v, cte, alias=False) for v in c.lineage.arguments] +[c.lineage.sort_concepts]
                dimension = self.render_concept_sql(
                    c.lineage.arguments[0], cte, alias=False
                )
                rendered_components = [
                    self.render_concept_sql(x.expr, cte, alias=False)
                    for x in c.lineage.order_by
                ]
                rval = f"{WINDOW_FUNCTION_MAP[WindowType.ROW_NUMBER](dimension, sort=','.join(rendered_components), order = 'desc')}"
            else:
                args = [
                    self.render_concept_sql(v, cte, alias=False)
                    if isinstance(v, Concept)
                    else self.render_literal(v)
                    for v in c.lineage.arguments
                ]
                if cte.group_to_grain:
                    rval = f"{FUNCTION_MAP[c.lineage.operator](args)}"
                else:
                    rval = f"{FUNCTION_GRAIN_MATCH_MAP[c.lineage.operator](args)}"
        # else if it's complex, just reference it from the source
        elif c.lineage:
            rval = f"{cte.source_map.get(c.address, INVALID_REFERENCE_STRING)}.{safe_quote(c.safe_address, self.QUOTE_CHARACTER)}"
        else:
            rval = f"{cte.source_map.get(c.address, INVALID_REFERENCE_STRING)}.{safe_quote(cte.get_alias(c), self.QUOTE_CHARACTER)}"

        if alias:
            return f"{rval} as {self.QUOTE_CHARACTER}{c.safe_address}{self.QUOTE_CHARACTER}"
        return rval

    def render_expr(
        self,
        e: Union[Expr, Conditional, Concept, str, int, bool],
        cte: Optional[CTE] = None,
    ) -> str:
        if isinstance(e, Comparison):
            return f"{self.render_expr(e.left, cte=cte)} {e.operator.value} {self.render_expr(e.right, cte=cte)}"
        elif isinstance(e, Conditional):
            return f"{self.render_expr(e.left, cte=cte)} {e.operator.value} {self.render_expr(e.right, cte=cte)}"
        elif isinstance(e, Function):
            if cte and cte.group_to_grain:
                return FUNCTION_MAP[e.operator](
                    [self.render_expr(z, cte=cte) for z in e.arguments]
                )
            return FUNCTION_GRAIN_MATCH_MAP[e.operator](
                [self.render_expr(z, cte=cte) for z in e.arguments]
            )
        elif isinstance(e, Concept):
            if cte:
                return self.render_concept_sql(e, cte, False)
                # return f"{cte.source_map[e.address]}.{self.QUOTE_CHARACTER}{cte.get_alias(e)}{self.QUOTE_CHARACTER}"
            return f"{self.QUOTE_CHARACTER}{e.safe_address}{self.QUOTE_CHARACTER}"
        elif isinstance(e, bool):
            return f"{1 if e else 0}"
        elif isinstance(e, str):
            return f"'{e}'"
        return str(e)

    def generate_ctes(
        self, query: ProcessedQuery, where_assignment: Dict[str, Conditional]
    ):

        return [
            CompiledCTE(
                name=cte.name,
                statement=self.SQL_TEMPLATE.render(
                    select_columns=[
                        self.render_concept_sql(c, cte) for c in cte.output_columns
                    ],
                    base=f"{cte.base_name} as {cte.base_alias}",
                    grain=cte.grain,
                    limit=None,
                    joins=[
                        render_join(join, self.QUOTE_CHARACTER)
                        for join in (cte.joins or [])
                    ],
                    where=self.render_expr(where_assignment[cte.name], cte)
                    if cte.name in where_assignment
                    else None,
                    group_by=[
                        self.render_concept_sql(c, cte, alias=False)
                        for c in unique(
                            cte.grain.components
                            + [
                                c
                                for c in cte.output_columns
                                if c.purpose == Purpose.PROPERTY
                                and c not in cte.grain.components
                            ],
                            "address",
                        )
                    ]
                    if cte.group_to_grain
                    else None,
                ),
            )
            for cte in query.ctes
        ]

    def generate_queries(
        self,
        environment: Environment,
        statements,
        hooks: Optional[List[BaseProcessingHook]] = None,
    ) -> List[ProcessedQuery]:
        output = []
        for statement in statements:
            if isinstance(statement, Select):
                output.append(process_query(environment, statement, hooks))
                # graph = generate_graph(environment, statement)
                # output.append(graph_to_query(environment, graph, statement))
        return output

    def compile_statement(self, query: ProcessedQuery) -> str:

        select_columns: list[str] = []
        cte_output_map = {}
        selected = set()
        output_addresses = [c.address for c in query.output_columns]

        # valid joins are anything that is a subset of the final grain
        output_ctes = [cte for cte in query.ctes if cte.grain.issubset(query.grain)]
        for cte in output_ctes:
            for c in cte.output_columns:
                if c.address not in selected and c.address in output_addresses:
                    select_columns.append(
                        f"{cte.name}.{safe_quote(c.safe_address, self.QUOTE_CHARACTER)}"
                    )
                    cte_output_map[c.address] = cte
                    selected.add(c.address)
        if not all([x in selected for x in output_addresses]):
            missing = [x for x in output_addresses if x not in selected]
            raise ValueError(
                f"Did not get all output addresses in select - missing: {missing}, have {selected}"
            )

        # where assignment
        where_assignment = {}
        output_where = False
        if query.where_clause:
            found = False
            filter = set([str(x.with_grain()) for x in query.where_clause.input])
            # if all the where clause items are lineage derivation
            # check if they match at output grain
            # TODO: handle if they don't all match
            # we need to force the filtering to happen after the initial CTE
            if all(
                [
                    x.derivation in (PurposeLineage.WINDOW, PurposeLineage.AGGREGATE)
                    for x in query.where_clause.input
                ]
            ):
                query_output = set([str(z) for z in query.output_columns])
                filter_at_output_grain = set(
                    [str(x.with_grain(query.grain)) for x in query.where_clause.input]
                )
                if filter_at_output_grain.issubset(query_output):
                    output_where = True
                    found = True
            if not found:
                for cte in output_ctes:
                    cte_filter = set([str(z.with_grain()) for z in cte.output_columns])
                    if filter.issubset(cte_filter):
                        # 2023-01-16 - removing related columns to look at output columns
                        # will need to backport pushing where columns into original output search
                        # if set([x.name for x in query.where_clause.input]).issubset(
                        #     [z.name for z in cte.related_columns]
                        # ):
                        where_assignment[cte.name] = query.where_clause.conditional
                        found = True
                        break

            if not found:
                raise NotImplementedError(
                    "Cannot generate complex query with filtering on grain that does not match any source."
                )
        for join in query.joins:

            if join.right_cte.name in where_assignment:
                # force filtering if the CTE has a where clause
                join.jointype = JoinType.INNER
                # if the left source is partial, make it a full join
            elif (
                join.left_cte.grain.issubset(query.grain)
                and join.left_cte.grain != query.grain
            ):
                join.jointype = JoinType.FULL
        compiled_ctes = self.generate_ctes(query, where_assignment)
        return self.SQL_TEMPLATE.render(
            select_columns=select_columns,
            base=query.base.name,
            joins=[render_join(join, self.QUOTE_CHARACTER) for join in query.joins],
            ctes=compiled_ctes,
            limit=query.limit,
            # move up to CTEs
            where=self.render_expr(
                query.where_clause.conditional
            )  # source_map=cte_output_map)
            if query.where_clause and output_where
            else None,
            order_by=[
                self.render_order_item(i, output_ctes) for i in query.order_by.items
            ]
            if query.order_by
            else None,
        )
