# from preql.compiler import compile
from preql.core.models import Select, Grain
from preql.parser import parse


def test_select():
    declarations = """
key user_id int metadata(description="the description");
property user_id.display_name string metadata(description="The display name ");
property user_id.about_me string metadata(description="User provided description");
key post_id int;


datasource posts (
    user_id: user_id,
    id: post_id
    )
    grain (post_id)
    address bigquery-public-data.stackoverflow.post_history
;


datasource users (
    id: user_id,
    display_name: display_name,
    about_me: about_me,
    )
    grain (user_id)
    address bigquery-public-data.stackoverflow.users
;


    """
    env, parsed = parse(declarations)

    q1 = """select
    user_id,
    about_me,
    count(post_id)->post_count
;"""
    env, parse_one = parse(q1, environment=env)

    select: Select = parse_one[-1]
    assert select.grain == Grain(components=[env.concepts["user_id"]])

    q2 = """select
    about_me,
    post_count
;"""
    env, parse_two = parse(q2, environment=env)

    select: Select = parse_two[-1]
    assert select.grain == Grain(components=[env.concepts["about_me"]])
