"""The example dialect: systemIdRef and unions keyed by system ID."""

import pytest

from reed.dialects.example import ExampleLowering
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
        '<group2 name="items" countValue="n"><integer name="v" bitCount="8"/></group2>'
    )

    assert field == Array("items", "n", Struct("items", (Int("v", 8),)))


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
