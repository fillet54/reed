"""Expansion: split messages into variants, one per union case, as a tree."""

import pytest

from reed.dialect import Dialect
from reed.dialects.example import EXAMPLE
from reed.expand import ExpandError, Variant, expand
from reed.ir import DEFAULT, Int, MessageRef, Struct, Union


def tree(body: str, *patterns: str, extra: str = "", dialect: Dialect | None = None):
    """The expansion tree of message "m"."""
    dialect = Dialect("test", expand=patterns) if dialect is None else dialect
    icds = dialect.lower([f'<icd><message name="m">{body}</message>{extra}</icd>'])
    roots = expand(icds, dialect)[""]
    return roots[0]


def names(variant: Variant) -> list[str]:
    """Every variant in the tree, depth first."""
    return [variant.name] + [n for child in variant.children for n in names(child)]


def find(variant: Variant, name: str) -> Variant:
    if variant.name == name:
        return variant
    for child in variant.children:
        try:
            return find(child, name)
        except KeyError:
            pass
    raise KeyError(name)


def field_names(struct: Struct) -> list[str]:
    return [getattr(f, "name", "(spare)") for f in struct.fields]


COMMANDS = (
    '<integer name="id" bitCount="8"/>'
    '<union name="body" discrimField="../id">'
    '<command name="a" discrimValue="1"><integer name="x" bitCount="8"/></command>'
    '<command name="b" discrimValue="2"><integer name="y" bitCount="8"/></command>'
    "</union>"
)


# --- one union --------------------------------------------------------------


def test_root_is_the_message_itself():
    root = tree(COMMANDS, "m/id")

    assert root.name == "m"
    assert root.id == ("m",)
    assert isinstance(root.message.body.fields[1], Union)


def test_listed_union_splits_into_one_variant_per_case():
    root = tree(COMMANDS, "m/id")

    assert [c.name for c in root.children] == ["m/a", "m/b"]
    assert [c.id for c in root.children] == [("m", 1), ("m", 2)]


def test_case_fields_go_in_a_group_named_after_the_union():
    a = find(tree(COMMANDS, "m/id"), "m/a")

    assert field_names(a.message.body) == ["id", "body"]
    body = a.message.body.fields[1]
    assert isinstance(body, Struct)
    assert field_names(body) == ["x"]


def test_discriminator_is_fixed_in_the_variant():
    b = find(tree(COMMANDS, "m/id"), "m/b")

    assert b.message.consts == (("id", 2),)
    assert isinstance(b.message.body.fields[0], Int)  # still in the layout


def test_variant_key_is_its_name_parts():
    b = find(tree(COMMANDS, "m/id"), "m/b")

    assert b.message.key == ("m", "b")
    assert b.message.name == "m"


def test_unlisted_union_is_not_split():
    root = tree(COMMANDS)

    assert root.children == ()


def test_unnamed_union_fields_go_into_the_parent():
    root = tree(
        '<integer name="id" bitCount="8"/>'
        '<union discrimField="../id">'
        '<command name="a" discrimValue="1"><integer name="x" bitCount="8"/></command>'
        "</union>",
        "m/id",
    )

    assert field_names(find(root, "m/a").message.body) == ["id", "x"]


# --- nesting ----------------------------------------------------------------


SUBCOMMANDS = (
    '<integer name="id" bitCount="8"/>'
    '<union name="body" discrimField="../id">'
    '<command name="a" discrimValue="5">'
    '  <integer name="subid" bitCount="8"/>'
    '  <union name="sub" discrimField="../subid">'
    '    <command name="x" discrimValue="10"><spare bitCount="8"/></command>'
    '    <command name="y" discrimValue="11"><spare bitCount="16"/></command>'
    "  </union>"
    "</command>"
    '<command name="b" discrimValue="6"/>'
    "</union>"
)


def test_subcommands_nest_under_their_command():
    root = tree(SUBCOMMANDS, "m/id", "m/body/*/subid")

    assert names(root) == ["m", "m/a", "m/a/x", "m/a/y", "m/b"]
    assert find(root, "m/a/y").id == ("m", 5, 11)


def test_nested_discriminator_is_fixed_at_its_variant_path():
    y = find(tree(SUBCOMMANDS, "m/id", "m/body/*/subid"), "m/a/y")

    assert y.message.consts == (("id", 5), ("body/subid", 11))
    body = y.message.body.fields[1]
    assert field_names(body) == ["subid", "sub"]
    assert isinstance(body.fields[1], Struct)


def test_intermediate_variant_keeps_the_inner_union():
    a = find(tree(SUBCOMMANDS, "m/id", "m/body/*/subid"), "m/a")

    assert isinstance(a.message.body.fields[1].fields[1], Union)


def test_without_the_outer_pattern_the_inner_union_is_not_split():
    root = tree(SUBCOMMANDS, "m/body/*/subid")

    assert names(root) == ["m"]


def test_wildcard_matches_exactly_one_level():
    assert names(tree(SUBCOMMANDS, "m/id", "m/*/subid")) == ["m", "m/a", "m/b"]


def test_sibling_unions_nest_in_field_order():
    root = tree(
        '<integer name="p" bitCount="8"/><integer name="q" bitCount="8"/>'
        '<union name="first" discrimField="../p">'
        '<command name="on" discrimValue="1"/><command name="off" discrimValue="0"/></union>'
        '<union name="second" discrimField="../q">'
        '<command name="hot" discrimValue="1"/><command name="cold" discrimValue="2"/></union>',
        "m/p",
        "m/q",
    )

    assert names(root) == [
        "m",
        "m/on", "m/on/hot", "m/on/cold",
        "m/off", "m/off/hot", "m/off/cold",
    ]  # fmt: skip
    assert find(root, "m/off/cold").id == ("m", 0, 2)


def test_default_case_is_a_variant_with_an_open_discriminator():
    root = tree(
        COMMANDS.replace("</union>", '<spare discrimValue="default" bitCount="8"/></union>'),
        "m/id",
    )

    default = find(root, "m/default")
    assert default.id == ("m", DEFAULT)
    assert default.message.consts == ()


# --- what is not expanded ---------------------------------------------------


def test_carried_message_is_a_leaf_that_links_to_it():
    root = tree(
        '<integer name="id" bitCount="8"/>'
        '<union name="body" discrimField="../id">'
        '<command name="truck" discrimValue="7" ref="payload"/></union>',
        "m/id",
        "payload/id",
        extra='<message name="payload"><integer name="id" bitCount="8"/>'
        '<union name="body" discrimField="../id">'
        '<command name="ping" discrimValue="1"/></union></message>',
    )

    truck = find(root, "m/truck")
    assert truck.children == ()
    (ref,) = truck.message.body.fields[1].fields
    assert isinstance(ref, MessageRef)
    assert ref.target == ("", "payload")


def test_payload_message_has_its_own_tree():
    dialect = Dialect("test", expand=("payload/id",))
    icds = dialect.lower(
        [
            '<icd><message name="payload"><integer name="id" bitCount="8"/>'
            '<union name="body" discrimField="../id">'
            '<command name="ping" discrimValue="1"/></union></message></icd>'
        ]
    )

    (payload,) = expand(icds, dialect)[""]
    assert names(payload) == ["payload", "payload/ping"]


def test_alias_prefix_limits_a_pattern_to_one_icd():
    body = COMMANDS
    a = '<icd><namespace alias="a">urn:a</namespace><message name="m">{}</message></icd>'
    b = '<icd><namespace alias="b">urn:b</namespace><message name="m">{}</message></icd>'
    dialect = Dialect("test", expand=("b:m/id",))

    trees = expand(dialect.lower([a.format(body), b.format(body)]), dialect)

    assert names(trees["urn:a"][0]) == ["m"]
    assert names(trees["urn:b"][0]) == ["m", "m/a", "m/b"]


# --- validation -------------------------------------------------------------


def test_listed_union_inside_an_array_is_an_error():
    dialect = Dialect("test", expand=("m/items/kind",))
    icds = dialect.lower(
        [
            '<icd><message name="m"><integer name="n" bitCount="4"/>'
            '<group name="items" countValue="../n">'
            '<integer name="kind" bitCount="4"/>'
            '<union name="v" discrimField="../kind">'
            '<command name="a" discrimValue="1"/></union>'
            "</group></message></icd>"
        ]
    )

    issues = [(i.severity, i.code, i.path) for i in dialect.validate(icds)]

    assert ("error", "expand-in-array", "m/items/v") in issues


def test_pattern_that_matches_nothing_is_a_warning():
    dialect = Dialect("test", expand=("m/id", "m/nope"))
    icds = dialect.lower([f'<icd><message name="m">{COMMANDS}</message></icd>'])

    issues = [(i.severity, i.code, i.message) for i in dialect.validate(icds)]

    assert issues == [("warning", "expand-unused", "expand pattern 'm/nope' matches no union")]


def test_pattern_for_a_message_that_is_not_loaded_is_fine():
    dialect = Dialect("test", expand=("m/id", "other/id"))
    icds = dialect.lower([f'<icd><message name="m">{COMMANDS}</message></icd>'])

    assert dialect.validate(icds) == []


def test_expansion_refuses_an_icd_with_errors():
    dialect = Dialect("test", expand=("m/id",))
    icds = dialect.lower(
        [
            '<icd><message name="m"><integer name="id" bitCount="8"/>'
            '<union name="body" discrimField="../id">'
            '<command name="a" discrimValue="1"/><command name="b" discrimValue="1"/>'
            "</union></message></icd>"
        ]
    )

    with pytest.raises(ExpandError, match="duplicate-case-value"):
        expand(icds, dialect)


# --- system IDs (example dialect) -------------------------------------------


def example_tree():
    icds = EXAMPLE.load(["tests/data/example-icd.xml"])  # and test-icd.xml, by import
    return {v.name: v for trees in expand(icds, EXAMPLE).values() for v in trees}


def test_example_icd_expands_commands_and_returns():
    trees = example_tree()

    assert names(trees["Command"]) == [
        "Command",
        "Command/power",
        "Command/gimbalTrim",
        "Command/spsBurn",
        "Command/storeAp",
        "Command/TestCommand",
        "Command/uplinkData",
        "Command/setCallsign",
        "Command/default",
    ]
    assert names(trees["Return"]) == [
        "Return",
        "Return/status",
        "Return/status/GNC",
        "Return/status/GNC.IMU.inner",
        "Return/status/GNC.IMU.middle",
        "Return/status/GNC.IMU.outer",
        "Return/status/SPS.propellant",
        "Return/status/SPS.propellant.fuel",
        "Return/status/SPS.propellant.oxidizer",
        "Return/status/EPS.fuelCell.fc1",
        "Return/status/EPS.fuelCell.fc2",
        "Return/status/EPS.fuelCell.fc3",
        "Return/fault",
        "Return/batteryVoltages",
        "Return/crewMessage",
    ]


def test_example_test_commands_have_their_own_tree():
    assert names(example_tree()["TestCommand"]) == [
        "TestCommand",
        "TestCommand/lampTest",
        "TestCommand/imuSelfTest",
        "TestCommand/memoryCheck",
    ]


def test_system_id_variant_fixes_the_whole_system_id():
    fc2 = find(example_tree()["Return"], "Return/status/EPS.fuelCell.fc2")

    assert fc2.id == ("Return", 1, (4, 1, 2))
    assert fc2.message.consts == (
        ("header/ReturnNumber", 1),
        ("ReturnBody/systemId", (4, 1, 2)),
    )
    assert field_names(fc2.message.body.fields[1]) == ["systemId", "(spare)", "detail"]
    assert field_names(fc2.message.body.fields[1].fields[2]) == ["output"]


def test_plain_system_id_field_is_not_split():
    power = find(example_tree()["Command"], "Command/power")

    assert power.children == ()


def test_a_variant_of_an_unnested_union_stays_unnested():
    variant = find(
        tree(
            '<integer name="id" bitCount="8"/>'
            '<union name="body" discrimField="../id" nestContents="false">'
            '<command name="a" discrimValue="1"><integer name="x" bitCount="8"/></command>'
            "</union>",
            "m/id",
        ),
        "m/a",
    )

    body = variant.message.body.fields[1]
    assert isinstance(body, Struct) and body.name == "body" and not body.nest
