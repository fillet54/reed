"""The example dialect: subsystemDef/subsystemIDRef and unions keyed by system ID."""

import pytest

from reed.dialect import BASE
from reed.dialects.example import EXAMPLE, ExampleLowering
from reed.ir import Array, Case, Domain, Int, Meta, Spare, Struct, Union
from reed.lower import LoweringError, lower_string

SYSTEM_ID_DEF = """
<group id="SubsystemType" xsi:type="subsystemDef" bitCount="21">
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


def test_subsystem_id_ref_is_three_seven_bit_fields():
    (field,) = lower_fields('<group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>')

    assert field.name == "systemId"
    assert field.fields == (Int("system", 7), Int("subsystem", 7), Int("unit", 7))
    assert field.declared_bits == 21
    assert field.meta == Meta(origin=("", "SubsystemType"))


def test_subsystem_id_ref_keeps_hierarchy_as_domain():
    (field,) = lower_fields('<group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>')

    assert field.domain == (
        Domain(
            "vehicle",
            1,
            (Domain("computer", 1, (Domain("left", 1), Domain("right", 2))),),
        ),
        Domain("fuel", 10),
    )


def test_plain_ref_to_a_subsystem_def_works_too():
    (field,) = lower_fields('<group name="systemId" ref="SubsystemType"/>')

    assert field.fields == (Int("system", 7), Int("subsystem", 7), Int("unit", 7))


def test_subsystem_id_ref_must_point_at_a_subsystem_def():
    icd = (
        '<icd><fieldDefs><group id="header"><spare bitCount="8"/></group></fieldDefs>'
        '<message name="m"><group name="id" ref="header" xsi:type="subsystemIDRef"/>'
        "</message></icd>"
    )

    with pytest.raises(LoweringError, match="subsystemIDRef 'header' is not a subsystemDef"):
        lower_string(icd, lowering=ExampleLowering)


def test_subsystem_id_ref_needs_a_ref():
    with pytest.raises(LoweringError, match="xsi:type subsystemIDRef needs a ref"):
        lower_fields('<group name="systemId" xsi:type="subsystemIDRef"/>')


def test_old_system_id_ref_type_is_gone():
    with pytest.raises(LoweringError, match="unknown xsi:type 'systemIdRef'"):
        lower_string(
            '<icd><fieldDefs><group id="s" xsi:type="systemIdRef" bitCount="21"/></fieldDefs>'
            '<message name="m"><group name="x" ref="s"/></message></icd>',
            lowering=ExampleLowering,
        )


def test_union_keyed_by_system_id_flattens_to_cases_per_level():
    _, union = lower_fields(
        '<group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>'
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
    (field,) = lower_fields('<group2 name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>')

    assert field.meta.origin == ("", "SubsystemType")


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


# --- commandkey: an integer -------------------------------------------------


def test_command_key_lowers_like_integer():
    (field,) = lower_fields('<commandkey name="uniqueKey" bitCount="16"/>')

    assert field == Int("uniqueKey", 16)


def test_command_key_keeps_integer_options():
    (field,) = lower_fields(
        '<commandkey name="uniqueKey" bitCount="8" signed="true">'
        "<label>Unique key</label></commandkey>"
    )

    assert field == Int("uniqueKey", 8, signed=True, meta=Meta(label="Unique key"))


def test_command_key_can_be_a_field_def():
    icd = lower_string(
        '<icd><fieldDefs><group id="header"><commandkey name="uniqueKey" bitCount="16"/>'
        '</group></fieldDefs><message name="m"><group name="header" ref="header"/></message></icd>',
        lowering=ExampleLowering,
    )

    (header,) = icd.messages[0].body.fields
    assert header.fields == (Int("uniqueKey", 16),)


def test_command_key_is_not_part_of_the_base_lowering():
    with pytest.raises(LoweringError, match="unknown element <commandkey>"):
        lower_string(
            '<icd><message name="m"><commandkey name="k" bitCount="16"/></message></icd>'
        )


# --- mpmCheck: an integer (a checksum field) --------------------------------


def test_mpm_check_lowers_like_integer():
    (field,) = lower_fields('<mpmCheck name="checksum" bitCount="16"/>')

    assert field == Int("checksum", 16)


def test_mpm_check_keeps_integer_options():
    (field,) = lower_fields(
        '<mpmCheck name="checksum" bitCount="16"><label>Checksum</label>'
        "<default>0</default></mpmCheck>"
    )

    assert field == Int("checksum", 16, meta=Meta(label="Checksum"), default=0)


def test_mpm_check_can_be_a_field_def():
    icd = lower_string(
        '<icd><fieldDefs><mpmCheck id="check" bitCount="16"/></fieldDefs>'
        '<message name="m"><mpmCheck name="checksum" ref="check"/></message></icd>',
        lowering=ExampleLowering,
    )

    assert icd.messages[0].body.fields == (Int("checksum", 16, meta=Meta(origin=("", "check"))),)


def test_mpm_check_is_not_part_of_the_base_lowering():
    with pytest.raises(LoweringError, match="unknown element <mpmCheck>"):
        lower_string('<icd><message name="m"><mpmCheck name="c" bitCount="16"/></message></icd>')


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


def test_unprotocolled_limit_counts_the_implicit_spare():
    # 16 + 385 = 401 bits, padded to 416: over the limit even though 401 rounds to 51 bytes.
    # 16 + 384 = 400 is exactly 25 words and fine (see above).
    assert command_issues(
        '<command name="big" discrimValue="1" protocolled="false"><spare bitCount="370"/>'
        '<spare bitCount="15"/></command>'
    ) == [("error", "unprotocolled-too-large", "m/body/big")]


def test_example_words_are_16_bits():
    icds = EXAMPLE.lower(['<icd><message name="m"/></icd>'])

    assert icds[""].layout.word_bits == 16


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


DUPLICATE_UNITS = """
<icd><fieldDefs>
  <group id="SubsystemType" xsi:type="subsystemDef" bitCount="21">
    <system name="vehicle" value="1"><subsystem name="computer" value="1">
      <unit name="left" value="1"/><unit name="center" value="1"/>
    </subsystem></system>
  </group>
</fieldDefs>
<message name="a"><group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/></message>
<message name="b">
  <group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>
  <union discrimField="../systemId">
    <system name="vehicle" value="1"><subsystem name="computer" value="1">
      <unit name="left" value="1"/><unit name="center" value="1"/>
    </subsystem></system>
  </union>
</message></icd>
"""


def test_duplicate_unit_values_are_reported_once_at_first_use():
    found = {(i.code, i.path) for i in EXAMPLE.validate(EXAMPLE.lower([DUPLICATE_UNITS]))}

    assert found == {
        # SubsystemType: left and center are both 1. Reported once, at the first
        # use of the fieldDef...
        ("duplicate-domain-value", "a/systemId"),
        # ...and the union keyed by it has two cases with value (1, 1, 1).
        ("duplicate-case-value", "b/(union)"),
    }


def test_example_icd_is_valid():
    with open("tests/data/example-icd.xml") as f:
        icds = EXAMPLE.lower([f.read()])

    assert EXAMPLE.validate(icds) == []


# --- <apCommand> --------------------------------------------------------------

AP_COMMANDS = (
    '<message name="Command"><integer name="CommandNumber" bitCount="8"/>'
    '<union name="CommandBody" discrimField="../CommandNumber">'
    '<command name="power" discrimValue="1"><integer name="on" bitCount="8"/></command>'
    '<command name="storeAp" discrimValue="2">'
    '  <integer name="CommandNumber" bitCount="8"/><apCommand name="stored"/>'
    "</command>"
    "</union></message>"
)


def ap_icd(extra: str = ""):
    return lower_string(f"<icd>{AP_COMMANDS}{extra}</icd>", lowering=ExampleLowering)


def test_ap_command_is_a_union_of_the_top_level_commands():
    icd = ap_icd(
        '<message name="schedule"><integer name="CommandNumber" bitCount="8"/>'
        '<apCommand name="cmd"/></message>'
    )

    _, cmd = icd.messages[1].body.fields
    assert isinstance(cmd, Union)
    assert (cmd.name, cmd.discrim) == ("cmd", "../CommandNumber")
    command_body = icd.messages[0].body.fields[1]
    assert [c.name for c in cmd.cases] == ["power", "storeAp"]
    assert cmd.cases[0] == command_body.cases[0]


def test_ap_command_discrim_field_can_be_given():
    icd = ap_icd(
        '<message name="schedule"><integer name="num" bitCount="8"/>'
        '<apCommand name="cmd" discrimField="../num"/></message>'
    )

    assert icd.messages[1].body.fields[1].discrim == "../num"


def test_ap_command_inside_a_command_holds_the_commands_one_level_deep():
    command_body = ap_icd().messages[0].body.fields[1]

    stored = command_body.cases[1].body.fields[1]
    assert [c.name for c in stored.cases] == ["power", "storeAp"]
    nested = stored.cases[1].body.fields[1]
    assert nested.cases == ()  # a stored command can't itself store one


def test_ap_command_round_trips():
    from reed.codec import decode, encode

    icds = EXAMPLE.lower(
        [
            f"<icd>{AP_COMMANDS}"
            '<message name="schedule"><integer name="CommandNumber" bitCount="8"/>'
            '<apCommand name="cmd"/></message></icd>'
        ]
    )
    schedule = icds[""].messages[1]
    value = {"CommandNumber": 1, "cmd": {"power": {"on": 7}}}

    data = encode(schedule, value, icds)

    assert data == bytes([1, 7])
    assert decode(schedule, data, icds) == value


def test_ap_command_without_its_command_message_is_an_error():
    with pytest.raises(LoweringError, match="apCommand: no message 'Command'"):
        lower_string(
            '<icd><message name="m"><integer name="CommandNumber" bitCount="8"/>'
            '<apCommand name="cmd"/></message></icd>',
            lowering=ExampleLowering,
        )


def test_ap_command_is_not_part_of_the_base_lowering():
    with pytest.raises(LoweringError, match="unknown element <apCommand>"):
        lower_string(
            '<icd><message name="m"><apCommand name="cmd"/></message></icd>', lowering=BASE.lowering
        )
