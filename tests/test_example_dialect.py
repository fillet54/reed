"""The example dialect: systemIdRef and unions keyed by system ID."""

import pytest

from reed.dialect import BASE
from reed.dialects.example import EXAMPLE, ExampleLowering
from reed.ir import Array, Case, Domain, Int, Meta, Spare, Struct, Union
from reed.lower import LoweringError, lower_string

SYSTEM_ID_DEF = """
<group id="systemIdRef" xsi:type="systemIdRef" bitCount="21">
  <system name="vehicle" value="1">
    <subsystem name="computer" value="1">
      <unit name="left" value="1"/>
      <unit name="right" value="2"/>
    </subsystem>
  </system>
  <system name="fuel" value="10"/>
</group>
"""


def lower_fields(body: str):
    icd = lower_string(
        f"<icd><fieldDefs>{SYSTEM_ID_DEF}</fieldDefs>"
        f'<message name="m">{body}</message></icd>',
        lowering=ExampleLowering,
    )
    return icd.messages[0].body.fields


def test_system_id_ref_is_three_seven_bit_fields():
    (field,) = lower_fields('<group name="systemId" ref="systemIdRef"/>')

    assert field.name == "systemId"
    assert field.fields == (Int("system", 7), Int("subsystem", 7), Int("unit", 7))
    assert field.declared_bits == 21
    assert field.meta == Meta(origin=("", "systemIdRef"))


def test_system_id_ref_keeps_hierarchy_as_domain():
    (field,) = lower_fields('<group name="systemId" ref="systemIdRef"/>')

    assert field.domain == (
        Domain(
            "vehicle",
            1,
            (Domain("computer", 1, (Domain("left", 1), Domain("right", 2))),),
        ),
        Domain("fuel", 10),
    )


def test_union_keyed_by_system_id_flattens_to_cases_per_level():
    _, union = lower_fields(
        '<group name="systemId" ref="systemIdRef"/>'
        '<union discrimField="../systemId">'
        '  <system name="vehicle" value="1">'
        '    <integer name="aliveCount" bitCount="16"/>'
        '    <subsystem name="computer" value="1">'
        '      <unit name="left" value="1"><integer name="version" bitCount="16"/></unit>'
        "    </subsystem>"
        "  </system>"
        "</union>"
    )

    assert union == Union(
        "",
        "../systemId",
        (
            Case("vehicle", "system", (1, 0, 0), Struct("vehicle", (Int("aliveCount", 16),))),
            Case("vehicle.computer", "subsystem", (1, 1, 0), Struct("computer", ())),
            Case("vehicle.computer.left", "unit", (1, 1, 1), Struct("left", (Int("version", 16),))),
        ),
    )


def test_example_dialect_still_lowers_plain_command_unions():
    (union,) = lower_fields(
        '<union name="body" discrimField="id">'
        '<command name="power" discrimValue="1"/></union>'
    )

    assert union.cases == (Case("power", "command", 1, Struct("power", ())),)


# --- group2: the example dialect's alias for group --------------------------


def test_group2_lowers_like_group():
    (field,) = lower_fields(
        '<group2 name="g" bitCount="8"><integer name="a" bitCount="8"/></group2>'
    )

    assert field == Struct("g", (Int("a", 8),), declared_bits=8)


def test_group2_can_ref_a_field_def():
    (field,) = lower_fields('<group2 name="systemId" ref="systemIdRef"/>')

    assert field.meta.origin == ("", "systemIdRef")


def test_group2_with_count_value_becomes_array():
    (field,) = lower_fields(
        '<group2 name="items" countValue="../n"><integer name="v" bitCount="8"/></group2>'
    )

    assert field == Array("items", "../n", Struct("items", (Int("v", 8),)))


def test_group2_can_be_a_field_def():
    icd = lower_string(
        '<icd><fieldDefs><group2 id="pair"><spare bitCount="2"/></group2></fieldDefs>'
        '<message name="m"><group name="p" ref="pair"/></message></icd>',
        lowering=ExampleLowering,
    )

    assert icd.messages[0].body.fields[0].fields == (Spare(2),)


def test_group2_is_not_part_of_the_base_lowering():
    with pytest.raises(LoweringError, match="unknown element <group2>"):
        lower_string('<icd><message name="m"><group2 name="g"/></message></icd>')


# --- protocolled commands: a dialect-specific rule --------------------------


def test_protocolled_attribute_is_kept_on_the_case():
    (union,) = lower_fields(
        '<union name="body" discrimField="id">'
        '<command name="a" discrimValue="1" protocolled="false"/>'
        '<command name="b" discrimValue="2"/></union>'
    )

    assert union.cases[0].meta.prop("protocolled") is False
    assert union.cases[1].meta.prop("protocolled") is None


def command_issues(commands: str):
    """Validate a 16-bit header followed by a union of `commands`."""
    icds = EXAMPLE.lower(
        [
            '<icd><message name="m"><integer name="id" bitCount="16"/>'
            f'<union name="body" discrimField="../id">{commands}</union></message></icd>'
        ]
    )
    return [(i.severity, i.code, i.path) for i in EXAMPLE.validate(icds)]


def test_unprotocolled_command_over_50_bytes_is_an_error():
    # 16-bit header + 392-bit body = 408 bits = 51 bytes: the whole message counts.
    assert command_issues(
        '<command name="big" discrimValue="1" protocolled="false"><spare bitCount="392"/></command>'
    ) == [("error", "unprotocolled-too-large", "m/body/big")]


def test_unprotocolled_command_within_50_bytes_is_fine():
    assert (
        command_issues(
            '<command name="ok" discrimValue="1" protocolled="false">'
            '<spare bitCount="384"/></command>'
        )
        == []
    )


def test_protocolled_command_can_be_larger():
    assert (
        command_issues(
            '<command name="big" discrimValue="1" protocolled="true">'
            '<spare bitCount="392"/></command>'
            '<command name="plain" discrimValue="2"><spare bitCount="392"/></command>'
        )
        == []
    )


def test_limit_only_counts_the_chosen_command():
    # A large sibling case doesn't count against the small unprotocolled one.
    assert (
        command_issues(
            '<command name="small" discrimValue="1" protocolled="false"/>'
            '<command name="huge" discrimValue="2"><spare bitCount="2000"/></command>'
        )
        == []
    )


def test_base_dialect_has_no_protocolled_rule():
    icds = BASE.lower(
        [
            '<icd><message name="m"><integer name="id" bitCount="16"/>'
            '<union name="body" discrimField="../id">'
            '<command name="big" discrimValue="1" protocolled="false"><spare bitCount="392"/>'
            "</command></union></message></icd>"
        ]
    )

    assert BASE.validate(icds) == []


# --- the whole example file -------------------------------------------------


def test_example_icd_reports_its_duplicate_unit_values():
    with open("tests/data/example-icd.xml") as f:
        icds = EXAMPLE.lower([f.read()])

    found = {(i.code, i.path) for i in EXAMPLE.validate(icds)}

    assert found == {
        # systemIdRef: left, center and right under vehicle/computer are all
        # value 1. Reported once, at the first use of the fieldDef.
        ("duplicate-domain-value", "command/commandBody/power/systemId"),
        # so the status union also has three cases with value (1, 1, 1)
        ("duplicate-case-value", "return/returnBody/status/(union)"),
    }
