import os
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, MutableMapping, TypeVar, List, Optional, Union, Set
from pydantic import BaseModel, validator, Field

from preql.core.enums import (
    DataType,
    Purpose,
    JoinType,
    Ordering,
    Modifier,
    FunctionType,
    FunctionClass,
    BooleanOperator,
    ComparisonOperator,
    WindowOrder,
    PurposeLineage,
)
from preql.core.exceptions import UndefinedConceptException
from preql.utility import unique

KT = TypeVar("KT")
VT = TypeVar("VT")


class Metadata(BaseModel):
    pass


class Concept(BaseModel):
    name: str
    datatype: DataType
    purpose: Purpose
    metadata: Optional[Metadata] = None
    lineage: Optional[Union["Function", "WindowItem"]] = None
    namespace: str = ""
    keys: Optional[List["Concept"]] = None
    grain: "Grain" = Field(default=None)

    @validator("lineage")
    def lineage_validator(cls, v):
        if v and not isinstance(v, (Function, WindowItem)):
            raise ValueError(v)
        return v

    @validator("metadata")
    def metadata_validation(cls, v):
        v = v or Metadata()
        return v

    @validator("namespace", pre=True, always=True)
    def namespace_enforcement(cls, v):
        if not v:
            return "default"
        return v

    def with_namespace(self, namespace: str) -> "Concept":
        return self.__class__(
            name=self.name,
            datatype=self.datatype,
            purpose=self.purpose,
            metadata=self.metadata,
            lineage=self.lineage.with_namespace(namespace) if self.lineage else None,
            grain=self.grain.with_namespace(namespace),
            namespace=namespace,
            keys=self.keys,
        )

    @validator("grain", pre=True, always=True)
    def parse_grain(cls, v, values):
        # this is silly - rethink how we do grains
        if not v and values.get("purpose", None) == Purpose.KEY:
            v = Grain(
                components=[
                    Concept(
                        namespace=values.get("namespace", "default"),
                        name=values["name"],
                        datatype=values["datatype"],
                        purpose=values["purpose"],
                        grain=Grain(),
                    )
                ]
            )
        elif not v:
            v = Grain(components=[])
        elif isinstance(v, Concept):
            v = Grain(components=[v])
        return v

    def __eq__(self, other: object):
        if not isinstance(other, Concept):
            return False
        return (
            self.name == other.name
            and self.datatype == other.datatype
            and self.purpose == other.purpose
            and self.namespace == other.namespace
            and self.grain == other.grain
        )

    def __str__(self):
        grain = ",".join([str(c.address) for c in self.grain.components])
        return f"{self.namespace}.{self.name}<{grain}>"

    @property
    def address(self) -> str:
        return f"{self.namespace}.{self.name}"

    @property
    def output(self) -> "Concept":
        return self

    @property
    def safe_address(self) -> str:
        return f"{self.namespace}_{self.name}"

    def with_grain(self, grain: Optional["Grain"] = None) -> "Concept":
        return self.__class__(
            name=self.name,
            datatype=self.datatype,
            purpose=self.purpose,
            metadata=self.metadata,
            lineage=self.lineage,
            grain=grain,
            namespace=self.namespace,
            keys=self.keys,
        )

    def with_default_grain(self) -> "Concept":
        if self.purpose == Purpose.KEY:
            # we need to make this abstract
            grain = Grain(components=[deepcopy(self).with_grain(Grain())], nested=True)
        elif self.purpose == Purpose.PROPERTY:
            components = []
            if self.keys:
                components = self.keys
            if self.lineage:
                for item in self.lineage.arguments:
                    if isinstance(item, Concept):
                        components += item.sources
            grain = Grain(components=components)
        else:
            grain = self.grain  # type: ignore
        return self.__class__(
            name=self.name,
            datatype=self.datatype,
            purpose=self.purpose,
            metadata=self.metadata,
            lineage=self.lineage,
            grain=grain,
            keys=self.keys,
            namespace=self.namespace,
        )

    @property
    def sources(self) -> List["Concept"]:
        if self.lineage:
            output = []
            for item in self.lineage.arguments:
                if isinstance(item, Concept):
                    output.append(item)
                    output += item.sources
            return output
        return []

    @property
    def input(self):
        return [self] + self.sources

    @property
    def derivation(self) -> PurposeLineage:
        if self.lineage and isinstance(self.lineage, WindowItem):
            return PurposeLineage.WINDOW
        elif (
            self.lineage
            and isinstance(self.lineage, Function)
            and self.lineage.operator in FunctionClass.AGGREGATE_FUNCTIONS.value
        ):
            return PurposeLineage.AGGREGATE
        return PurposeLineage.BASIC


@dataclass(eq=True)
class ColumnAssignment:
    alias: str
    concept: Concept
    modifiers: List[Modifier] = field(default_factory=list)

    def is_complete(self):
        return Modifier.PARTIAL not in self.modifiers

    def with_namespace(self, namespace: str) -> "ColumnAssignment":
        # this breaks assignments
        # TODO: figure out why
        return self
        # return ColumnAssignment(
        #     alias=self.alias,
        #     concept=self.concept.with_namespace(namespace),
        #     modifiers=self.modifiers,
        # )


@dataclass(eq=True, frozen=True)
class Statement:
    pass


@dataclass(eq=True, frozen=True)
class Function:
    operator: FunctionType
    arguments: List[Union[Concept, str, float, int, DataType]]
    output_datatype: DataType
    output_purpose: Purpose
    valid_inputs: Optional[Union[Set[DataType], List[Set[DataType]]]] = None
    arg_count: int = field(default=1)

    def __post_init__(self):
        from preql.parsing.exceptions import ParseError

        arg_count = len(self.arguments)
        if not arg_count <= self.arg_count:
            raise ParseError(
                f"Incorrect argument count to {self.operator.name} function, expects {self.arg_count}, got {arg_count}"
            )
        for arg in self.arguments:
            if isinstance(arg, Function):
                raise ParseError(
                    f"Anonymous function calls not allowed; map function to a concept, then pass in. {arg.operator.name} being passed into {self.operator.name}"
                )
        valid_inputs = self.valid_inputs
        # if all arguments need to be the same type
        # turn this into an array for validation
        if isinstance(valid_inputs, set):
            valid_inputs = [valid_inputs for _ in self.arguments]
        elif not valid_inputs:
            return
        for idx, arg in enumerate(self.arguments):

            if isinstance(arg, Concept) and not arg.datatype in valid_inputs[idx]:
                raise TypeError(
                    f"Invalid input datatype {arg.datatype} passed into {self.operator.name} from concept {arg.name}"
                )

            for ptype, dtype in [
                [str, DataType.STRING],
                [int, DataType.INTEGER],
                [float, DataType.FLOAT],
            ]:
                if isinstance(arg, ptype) and dtype in valid_inputs[idx]:
                    # attempt to exit early to avoid checking all types
                    break
                elif isinstance(arg, ptype):
                    raise TypeError(
                        f"Invalid {dtype} constant passed into {self.operator.name} {arg}"
                    )

    def with_namespace(self, namespace: str) -> "Function":
        return Function(
            operator=self.operator,
            arguments=[
                c.with_namespace(namespace) if isinstance(c, Concept) else c
                for c in self.arguments
            ],
            output_datatype=self.output_datatype,
            output_purpose=self.output_purpose,
            valid_inputs=self.valid_inputs,
        )


@dataclass(eq=True)
class ConceptTransform:
    function: Function
    output: Concept

    @property
    def input(self) -> List[Concept]:
        return [v for v in self.function.arguments if isinstance(v, Concept)]


@dataclass
class Window:
    count: int
    window_order: WindowOrder

    def __str__(self):
        return f"Window<{self.window_order}>"


class WindowItem(BaseModel):
    content: Concept
    order_by: List["OrderItem"]

    def with_namespace(self, namespace: str) -> "WindowItem":
        return WindowItem(
            content=self.content.with_namespace(namespace),
            order_by=[x.with_namespace(self.namespace) for x in self.order_by],
        )

    @property
    def arguments(self) -> List[Concept]:
        output = [self.content]
        for order in self.order_by:
            output += [order.output]
        return output

    @property
    def output(self) -> Concept:
        if isinstance(self.content, ConceptTransform):
            return self.content.output
        return self.content

    @output.setter
    def output(self, value):
        if isinstance(self.content, ConceptTransform):
            self.content.output = value
        else:
            self.content = value

    @property
    def input(self) -> List[Concept]:
        return self.content.input + [v.input for v in self.order_by]

    @property
    def output_datatype(self):
        return self.content.datatype

    @property
    def output_purpose(self):
        return self.content.purpose


@dataclass(eq=True)
class SelectItem:
    content: Union[Concept, ConceptTransform]

    @property
    def output(self) -> Concept:
        if isinstance(self.content, ConceptTransform):
            return self.content.output
        elif isinstance(self.content, WindowItem):
            return self.content.output
        return self.content

    @property
    def input(self) -> List[Concept]:
        return self.content.input


@dataclass(eq=True)
class OrderItem:
    expr: Concept
    order: Ordering

    def with_namespace(self, namespace: str) -> "OrderItem":
        return OrderItem(expr=self.expr.with_namespace(namespace), order=self.order)

    @property
    def input(self):
        return self.expr.input

    @property
    def output(self):
        return self.expr.output


@dataclass(eq=True, frozen=True)
class OrderBy:
    items: List[OrderItem]


@dataclass(eq=True)
class Select:
    selection: Union[List[SelectItem], List[Union[Concept, ConceptTransform]]]
    where_clause: Optional["WhereClause"] = None
    order_by: Optional[OrderBy] = None
    limit: Optional[int] = None

    def __post_init__(self):
        final = []
        for item in self.selection:
            if isinstance(item, (Concept, ConceptTransform)):
                final.append(SelectItem(item))
            else:
                final.append(item)
        self.selection = final

    @property
    def input_components(self) -> List[Concept]:
        output = set()
        output_list = []
        for item in self.selection:
            for concept in item.input:
                if concept.name in output:
                    continue
                output.add(concept.name)
                output_list.append(concept)
        if self.where_clause:
            for concept in self.where_clause.input:
                if concept.name in output:
                    continue
                output.add(concept.name)
                output_list.append(concept)

        return output_list

    @property
    def output_components(self) -> List[Concept]:
        output = []
        for item in self.selection:
            output.append(item.output)
        return output

    @property
    def all_components(self) -> List[Concept]:
        return self.input_components + self.output_components + self.grain.components

    @property
    def grain(self) -> "Grain":
        output = []
        for item in self.output_components:
            if item.purpose == Purpose.KEY:
                output.append(item)
        if self.where_clause:
            for item in self.where_clause.input:
                if item.purpose == Purpose.KEY:
                    output.append(item)
                # elif item.purpose == Purpose.PROPERTY and item.grain:
                #     output += item.grain.components
            # TODO: handle other grain cases
            # new if block be design
        # add back any purpose that is not at the grain
        # if a query already has the key of the property in the grain
        # we want to group to that grain and ignore the property, which is a derivation
        # otherwise, we need to include property as the group by
        for item in self.output_components:

            if item.purpose == Purpose.PROPERTY and not item.grain.issubset(
                Grain(components=unique(output, "address"))
            ):
                output.append(item)
        return Grain(components=unique(output, "address"))


@dataclass(eq=True, frozen=True)
class Address:
    location: str


@dataclass(eq=True, frozen=True)
class Query:
    text: str


class Grain(BaseModel):
    components: List[Concept] = Field(default_factory=list)
    nested: bool = False

    def __init__(self, **kwargs):
        if not kwargs.get("nested", False):
            kwargs["components"] = [
                c.with_default_grain() for c in kwargs.get("components", [])
            ]
        super().__init__(**kwargs)

    def __str__(self):
        if self.abstract:
            return "Grain<Abstract>"
        return "Grain<" + ",".join([c.address for c in self.components]) + ">"

    def with_namespace(self, namespace: str) -> "Grain":

        return Grain(
            components=[c.with_namespace(namespace) for c in self.components],
            nested=self.nested,
        )

    @property
    def abstract(self):
        return not self.components

    @property
    def set(self):
        return set([c.address for c in self.components])

    def __eq__(self, other: object):
        if not isinstance(other, Grain):
            return False
        return self.set == other.set

    def issubset(self, other: "Grain"):
        return self.set.issubset(other.set)

    def isdisjoint(self, other: "Grain"):
        return self.set.isdisjoint(other.set)

    def intersection(self, other: "Grain") -> "Grain":
        intersection = self.set.intersection(other.set)
        components = [i for i in self.components if i.name in intersection]
        return Grain(components=components)

    def __add__(self, other: "Grain"):
        components = []
        for clist in [self.components, other.components]:
            for component in clist:
                if component in components:
                    continue
                components.append(component)
        return Grain(components=components)

    def __radd__(self, other):
        if other == 0:
            return self
        else:
            return self.__add__(other)


@dataclass
class GrainWindow:
    window: Window
    sort_concepts: List[Concept]

    def __str__(self):
        return (
            "GrainWindow<"
            + ",".join([c.address for c in self.sort_concepts])
            + f":{str(self.window)}>"
        )


@dataclass
class Datasource:
    identifier: str
    columns: List[ColumnAssignment]
    address: Union[Address, str]
    grain: Grain = field(default_factory=lambda: Grain(components=[]))
    namespace: Optional[str] = ""

    def __add__(self, other):
        if not other == self:
            raise ValueError
        return self

    def __str__(self):
        return f"{self.namespace}.{self.identifier}@<{self.grain}>"

    def __hash__(self):
        return (self.namespace + self.identifier).__hash__()

    def __post_init__(self):
        # if a user skips defining a grain, use the defined keys
        if not self.grain or not self.grain.components:
            self.grain = Grain(
                components=[
                    deepcopy(v).with_grain(Grain())
                    for v in self.concepts
                    if v.purpose == Purpose.KEY
                ]
            )
        if isinstance(self.address, str):
            self.address = Address(location=self.address)
        if not self.namespace:
            self.namespace = ""

    def with_namespace(self, namespace: str):
        return Datasource(
            identifier=self.identifier,
            namespace=namespace,
            grain=self.grain.with_namespace(namespace),
            address=self.address,
            columns=[c.with_namespace(namespace) for c in self.columns],
        )

    @property
    def concepts(self) -> List[Concept]:
        return [c.concept for c in self.columns]

    @property
    def full_concepts(self) -> List[Concept]:
        return [c.concept for c in self.columns if Modifier.PARTIAL not in c.modifiers]

    @property
    def output_concepts(self) -> List[Concept]:
        return self.concepts

    @property
    def partial_concepts(self) -> List[Concept]:
        return [c.concept for c in self.columns if Modifier.PARTIAL in c.modifiers]

    def get_alias(
        self, concept: Concept, use_raw_name: bool = True, force_alias: bool = False
    ) -> Optional[str]:
        # 2022-01-22
        # this logic needs to be refined.
        # if concept.lineage:
        # #     return None
        for x in self.columns:
            if x.concept.with_grain(concept.grain) == concept:
                if use_raw_name:
                    return x.alias
                return concept.safe_address
        existing = [str(c.concept.with_grain(self.grain)) for c in self.columns]
        raise ValueError(
            f"Concept {concept} not found on {self.identifier}; have {existing}."
        )

    @property
    def name(self) -> str:
        return self.identifier
        # TODO: namespace all references
        # return f'{self.namespace}_{self.identifier}'

    @property
    def safe_location(self) -> str:
        if isinstance(self.address, Address):
            return self.address.location
        return self.address


@dataclass(eq=True)
class JoinedDataSource:
    concepts: List[Concept]
    source_map: Dict[str, "CTE"]
    grain: Grain
    address: Address
    # base: Datasource
    joins: List["Join"]

    @property
    def datasources(self) -> List[Datasource]:
        datasources = []
        for item in self.source_map.values():
            datasources.append(item.source)

        return unique(datasources, "identifier")

    @property
    def identifier(self) -> str:
        return "_join_".join([d.name for d in self.datasources])

    def get_alias(self, concept: Concept):
        for x in self.datasources:
            try:
                return x.get_alias(concept.with_grain(x.grain))
            except ValueError as e:
                from preql.constants import logger

                logger.error(e)
                continue
        existing = [str(c) for c in self.concepts]
        raise ValueError(
            f"Concept {str(concept)} not found on {self.identifier}; have {existing}."
        )


@dataclass
class BaseJoin:
    left_datasource: Union[Datasource, "QueryDatasource"]
    right_datasource: Union[Datasource, "QueryDatasource"]
    concepts: List[Concept]
    join_type: JoinType

    def __post_init__(self):
        for concept in self.concepts:
            for ds in [self.left_datasource, self.right_datasource]:
                if concept.address not in [c.address for c in ds.output_concepts]:
                    raise SyntaxError(f"Invalid join, missing {concept} on {ds.name}")

    @property
    def unique_id(self) -> str:
        # TODO: include join type?
        return (
            self.left_datasource.name
            + self.right_datasource.name
            + self.join_type.value
        )

    def __str__(self):
        return f'{self.join_type.value} JOIN {self.left_datasource.identifier} and {self.right_datasource.identifier} on {",".join([str(k) for k in self.concepts])}'


@dataclass(eq=True)
class QueryDatasource:
    input_concepts: List[Concept]
    output_concepts: List[Concept]
    source_map: Dict[str, Set[Union[Datasource, "QueryDatasource"]]]
    datasources: List[Union[Datasource, "QueryDatasource"]]
    grain: Grain
    joins: List[BaseJoin]
    limit: Optional[int] = None
    filter_concepts: List[Concept] = field(default_factory=list)

    def __str__(self):
        return f"{self.identifier}@<{self.grain}>"

    def validate(self):
        # validate this was successfully built.
        for concept in self.output_concepts:
            self.get_alias(concept.with_grain(self.grain))

    def __hash__(self):
        return (self.identifier).__hash__()

    @property
    def concepts(self):
        return self.output_concepts

    @property
    def name(self):
        return self.identifier

    def __post_init__(self):
        self.output_concepts = unique(self.output_concepts, "address")
        self.input_concepts = unique(self.input_concepts, "address")
        self.filter_concepts = unique(self.filter_concepts, "address")

    def __add__(self, other):
        if not isinstance(other, QueryDatasource):
            raise ValueError
        if not other.grain == self.grain:
            raise ValueError

        return QueryDatasource(
            input_concepts=unique(
                self.input_concepts + other.input_concepts, "address"
            ),
            output_concepts=unique(
                self.output_concepts + other.output_concepts, "address"
            ),
            source_map={**self.source_map, **other.source_map},
            datasources=self.datasources,
            grain=self.grain,
            joins=unique(self.joins + other.joins, "unique_id"),
            filter_concepts=unique(
                self.filter_concepts + other.filter_concepts, "address"
            ),
        )

    @property
    def identifier(self) -> str:
        grain = "_".join(
            [str(c.address).replace(".", "_") for c in self.grain.components]
        )
        return "_".join([d.name for d in self.datasources]) + (
            f"_at_{grain}" if grain else "_at_abstract"
        )
        # return #str(abs(hash("from_"+"_with_".join([d.name for d in self.datasources]) + ( f"_at_grain_{grain}" if grain else "" ))))

    def get_alias(
        self, concept: Concept, use_raw_name: bool = False, force_alias: bool = False
    ):
        # if we should use the raw datasource name to access
        use_raw_name = (
            True
            if (len(self.datasources) == 1 or use_raw_name) and not force_alias
            else False
        )
        for x in self.datasources:
            # query datasources should be referenced by their alias, always
            force_alias = isinstance(x, QueryDatasource)
            try:
                return x.get_alias(
                    concept.with_grain(self.grain),
                    use_raw_name,
                    force_alias=force_alias,
                )
            except ValueError as e:
                from preql.constants import logger

                logger.debug(e)
                continue
        existing = [c.with_grain(self.grain) for c in self.output_concepts]
        if concept in existing:
            return concept.name

        existing_str = [str(c) for c in existing]
        datasources = [ds.identifier for ds in self.datasources]
        raise ValueError(
            f"Concept {str(concept)} not found on {self.identifier}; have {existing_str} from {datasources}."
        )

    @property
    def safe_location(self):
        return self.identifier


@dataclass
class Comment:
    text: str


@dataclass
class CTE:
    name: str
    source: "QueryDatasource"  # TODO: make recursive
    # output columns are what are selected/grouped by
    output_columns: List[Concept]
    source_map: Dict[str, str]
    # related columns include all referenced columns
    related_columns: List[Concept]
    # filter columns are specific output columns for filtering
    # to support filtering before aggregation to grain
    filter_columns: List[Concept]
    grain: Grain
    base: bool = False
    group_to_grain: bool = False
    parent_ctes: List["CTE"] = field(default_factory=list)
    joins: List["Join"] = field(default_factory=list)

    def __add__(self, other: "CTE"):
        if not self.grain == other.grain:
            error = f"Attempting to merge two ctes of different grains {self.name} {other.name} grains {self.grain} {other.grain}"
            raise ValueError(error)

        self.parent_ctes = merge_ctes(self.parent_ctes + other.parent_ctes)

        self.source_map = {**self.source_map, **other.source_map}

        self.output_columns = unique(
            self.output_columns + other.output_columns, "address"
        )
        self.joins = unique(self.joins + other.joins, "unique_id")
        self.related_columns = unique(
            self.related_columns + other.related_columns, "address"
        )
        self.filter_columns = unique(
            self.filter_columns + other.filter_columns, "address"
        )
        return self

    @property
    def base_name(self) -> str:
        # if this cte selects from a single datasource, select right from it
        if len(self.source.datasources) == 1 and isinstance(
            self.source.datasources[0], Datasource
        ):
            return self.source.datasources[0].safe_location
        # if we have ctes, we should reference those
        elif self.joins and len(self.joins) > 0:
            return self.joins[0].left_cte.name
        elif self.parent_ctes:  # and len(self.parent_ctes) == 1:
            return self.parent_ctes[0].name
        # return self.source_map.values()[0]
        return self.source.name

    @property
    def base_alias(self) -> str:
        if len(self.source.datasources) == 1:
            if isinstance(self.source.datasources[0], QueryDatasource):
                return self.parent_ctes[0].name
            return self.source.datasources[0].name
        if self.joins:
            return self.joins[0].left_cte.name
        return self.name

    def get_alias(self, concept: Concept) -> str:
        error = ValueError(
            f"Error: alias not found looking for alias for concept {concept}"
        )
        for cte in [self] + self.parent_ctes:
            try:
                return cte.source.get_alias(concept)
            except ValueError as e:
                if not error:
                    error = e
        return "INVALID_ALIAS"
        raise error


def merge_ctes(ctes: List[CTE]) -> List[CTE]:
    final_ctes_dict: Dict[str, CTE] = {}
    # merge CTEs
    for cte in ctes:
        if cte.name not in final_ctes_dict:
            final_ctes_dict[cte.name] = cte
        else:
            final_ctes_dict[cte.name] = final_ctes_dict[cte.name] + cte
    final_ctes = list(final_ctes_dict.values())
    return final_ctes


@dataclass
class CompiledCTE:
    name: str
    statement: str


@dataclass
class JoinKey:
    concept: Concept

    def __str__(self):
        return str(self.concept)


@dataclass
class Join:
    left_cte: CTE
    right_cte: CTE
    jointype: JoinType
    joinkeys: List[JoinKey]

    @property
    def unique_id(self) -> str:
        return self.left_cte.name + self.right_cte.name + self.jointype.value

    def __str__(self):
        return f'{self.jointype.value} JOIN {self.left_cte.name} and {self.right_cte.name} on {",".join([str(k) for k in self.joinkeys])}'


class EnvironmentConceptDict(dict, MutableMapping[KT, VT]):
    def __getitem__(self, key, line_no=None):
        try:
            return super(EnvironmentConceptDict, self).__getitem__(key)
        except KeyError as e:
            if line_no:
                raise UndefinedConceptException(
                    f"line: {line_no} undefined concept: {str(e)}"
                )
            raise UndefinedConceptException(str(e))


@dataclass
class Environment:
    concepts: EnvironmentConceptDict[str, Concept] = field(
        default_factory=EnvironmentConceptDict
    )
    datasources: Dict[str, Datasource] = field(default_factory=dict)
    namespace: Optional[str] = None
    working_path: str = field(default_factory=lambda: os.getcwd())


@dataclass
class Expr:
    name: str = ""

    @property
    def input(self) -> List[Concept]:
        output: List[Concept] = []
        return output

    @property
    def safe_address(self):
        return ""

    @property
    def address(self):
        return ""


@dataclass
class Comparison:
    left: Union[Concept, Expr, "Conditional"]
    right: Union[Concept, Expr, "Conditional"]
    operator: ComparisonOperator

    @property
    def input(self) -> List[Concept]:
        output: List[Concept] = []
        if isinstance(self.left, (Concept,)):
            output += [self.left]
        elif isinstance(self.left, (Concept, Expr, Conditional)):
            output += self.left.input
        if isinstance(self.right, (Concept,)):
            output += [self.right]
        if isinstance(self.right, (Concept, Expr, Conditional)):
            output += self.right.input
        return output


@dataclass
class Conditional:
    left: Union[Concept, Expr, "Conditional"]
    right: Union[Concept, Expr, "Conditional"]
    operator: BooleanOperator

    @property
    def input(self) -> List[Concept]:
        """Return concepts directly referenced in where clause"""
        output = []
        if isinstance(self.left, Concept):
            output.append(self.left)
        else:
            output += self.left.input
        if isinstance(self.right, Concept):
            output.append(self.right)
        else:
            output += self.right.input
        return output


@dataclass
class WhereClause:
    conditional: Conditional

    @property
    def input(self) -> List[Concept]:
        return self.conditional.input

    @property
    def grain(self) -> Grain:
        output = []
        for item in self.input:
            if item.purpose == Purpose.KEY:
                output.append(item)
            elif item.purpose == Purpose.PROPERTY:
                output += item.grain.components
        return Grain(components=list(set(output)))


# TODO: combine with CTEs
# CTE contains procesed query?
# or CTE references CTE?
@dataclass
class ProcessedQuery:
    output_columns: List[Concept]
    ctes: List[CTE]
    base: CTE
    joins: List[Join]
    grain: Grain
    limit: Optional[int] = None
    where_clause: Optional[WhereClause] = None
    order_by: Optional[OrderBy] = None
    # base:Dataset


@dataclass
class Limit:
    count: int


Concept.update_forward_refs()
Grain.update_forward_refs()
WindowItem.update_forward_refs()
