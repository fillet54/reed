"""Lowering one element at a time: XML snippet in, IR node out."""

from dataclasses import replace

import pytest

from reed.ir import (
    DEFAULT,
    VARIABLE,
    Bytes,
    Array,
    Case,
    Enum,
    Float,
    Int,
    Message,
    MessageRef,
    Meta,
    NamedValue,
    Scaled,
    Spare,
    String,
    Struct,
    Union,
)
from reed.lower import Lowering, LoweringError, lower_many, lower_string


def lower_fields(xml: str, defs: str = "", extra: str = ""):
    """Lower `xml` as the body of a message named "m"; return its fields."""
    icd = lower_string(
        f"<icd><fieldDefs>{defs}</fieldDefs>"
        f'<message name="m">{xml}</message>{extra}</icd>'
    )
    return icd.messages[0].body.fields


def lower_field(xml: str, **kwargs):
    (field,) = lower_fields(xml, **kwargs)
    return field


# --- messages ---------------------------------------------------------------


def test_message_becomes_struct_keyed_by_name():
    icd = lower_string('<icd><message name="m"><spare bitCount="4"/></message></icd>')

    assert icd.messages == (Message(key=("m",), body=Struct("m", (Spare(4),))),)


def test_messages_keep_document_order():
    icd = lower_string('<icd><message name="b"/><message name="a"/></icd>')

    assert [m.key for m in icd.messages] == [("b",), ("a",)]


# --- integer ----------------------------------------------------------------


def test_integer():
    assert lower_field('<integer name="id" bitCount="16"/>') == Int("id", 16)


def test_integer_label_and_description_go_to_meta():
    field = lower_field(
        '<integer name="id" bitCount="16">'
        "<label>Identifier</label><description>Message id</description>"
        "</integer>"
    )

    assert field.meta == Meta(label="Identifier", description="Message id")


def test_integer_named_values_become_enum():
    field = lower_field(
        '<integer name="mode" bitCount="1">'
        "<namedValues>"
        '<namedValue name="ON" value="1"/><namedValue name="OFF" value="0"/>'
        "</namedValues></integer>"
    )

    assert field.enum == Enum((NamedValue("ON", 1), NamedValue("OFF", 0)))


def test_integer_restricted_named_values():
    field = lower_field(
        '<integer name="mode" bitCount="1">'
        '<namedValues restrict="true"><namedValue name="ON" value="1"/></namedValues>'
        "</integer>"
    )

    assert field.enum.restrict is True


def test_integer_is_unsigned_by_default():
    assert lower_field('<integer name="v" bitCount="8"/>').signed is False


def test_integer_signed():
    field = lower_field('<integer name="v" bitCount="8" signed="true"/>')

    assert field == Int("v", 8, signed=True)


def test_integer_signed_must_be_true_or_false():
    with pytest.raises(LoweringError, match="signed.*'1'"):
        lower_field('<integer name="v" bitCount="8" signed="1"/>')


def test_integer_requires_bit_count():
    with pytest.raises(LoweringError, match="bitCount.*<integer"):
        lower_field('<integer name="id"/>')


def test_integer_bit_count_must_be_fixed():
    with pytest.raises(LoweringError, match="bitCount"):
        lower_field('<integer name="id" bitCount="variable"/>')


# --- scalar -----------------------------------------------------------------


def test_scalar():
    field = lower_field('<scalar name="freq" bitCount="16" scale="1000" offset="2500"/>')

    assert field == Scaled("freq", 16, scale=1000.0, offset=2500.0)


def test_scalar_defaults_to_identity_scaling():
    assert lower_field('<scalar name="v" bitCount="8"/>') == Scaled("v", 8)


def test_scalar_is_unsigned_by_default():
    assert lower_field('<scalar name="v" bitCount="8"/>').signed is False


def test_scalar_signed():
    field = lower_field('<scalar name="v" bitCount="8" signed="true"/>')

    assert field == Scaled("v", 8, signed=True)


def test_scalar_signed_must_be_true_or_false():
    with pytest.raises(LoweringError, match="signed.*'yes'"):
        lower_field('<scalar name="v" bitCount="8" signed="yes"/>')


# --- string -----------------------------------------------------------------


def test_fixed_string():
    assert lower_field('<string name="s" bitCount="64"/>') == String("s", 64)


def test_variable_string():
    assert lower_field('<string name="s" bitCount="variable"/>') == String("s", VARIABLE)


def test_string_needs_a_bit_count():
    with pytest.raises(LoweringError, match="missing required attribute 'bitCount' on <string name='s'>"):
        lower_field('<string name="s"/>')


def test_string_can_be_a_field_def():
    field = lower_field('<string name="callsign" ref="name"/>', defs='<string id="name" bitCount="32"/>')

    assert (field.name, field.bits) == ("callsign", 32)


# --- spare ------------------------------------------------------------------


def test_spare():
    assert lower_field('<spare bitCount="10"/>') == Spare(10)


def test_spare_name_is_optional_and_ignored():
    assert lower_field('<spare name="pad" bitCount="10"/>') == Spare(10)


def test_spare_can_be_a_field_def():
    field = lower_field('<spare ref="pad16"/>', defs='<spare id="pad16" bitCount="16"/>')

    assert field == Spare(16)


def test_ref_to_a_spare_def_can_give_it_a_name_and_label():
    field = lower_field(
        '<spare name="gap" ref="pad16"><label>Gap</label></spare>',
        defs='<spare id="pad16" bitCount="16"/>',
    )

    assert field == Spare(16)


def test_ref_can_override_a_spares_bit_count():
    field = lower_field('<spare ref="pad16" bitCount="4"/>', defs='<spare id="pad16" bitCount="16"/>')

    assert field == Spare(4)


# --- group ------------------------------------------------------------------


def test_group_becomes_struct():
    field = lower_field(
        '<group name="g"><integer name="a" bitCount="4"/><spare bitCount="4"/></group>'
    )

    assert field == Struct("g", (Int("a", 4), Spare(4)))


def test_group_declared_bit_count():
    field = lower_field('<group name="g" bitCount="8"><spare bitCount="8"/></group>')

    assert field.declared_bits == 8


def test_group_variable_bit_count():
    field = lower_field('<group name="g" bitCount="variable"><spare bitCount="8"/></group>')

    assert field.declared_bits is VARIABLE


def test_group_with_count_value_becomes_array():
    field = lower_field(
        '<group name="items" countValue="num_items">'
        '<integer name="value" bitCount="8"/></group>'
    )

    assert field == Array("items", "num_items", Struct("items", (Int("value", 8),)))


# --- union ------------------------------------------------------------------


def test_union_cases_by_discriminator_value():
    field = lower_field(
        '<union name="body" discrimField="../header/id">'
        '<command name="power" discrimValue="1"><spare bitCount="4"/></command>'
        '<return name="status" discrimValue="2"/>'
        "</union>"
    )

    assert field == Union(
        "body",
        "../header/id",
        (
            Case("power", "command", 1, Struct("power", (Spare(4),))),
            Case("status", "return", 2, Struct("status", ())),
        ),
    )


def test_union_requires_discriminator():
    with pytest.raises(LoweringError, match="discrimField"):
        lower_field('<union name="body"/>')


def test_union_case_requires_discriminator_value():
    with pytest.raises(LoweringError, match="discrimValue"):
        lower_field('<union name="body" discrimField="x"><command name="c"/></union>')


# --- default cases ----------------------------------------------------------


def test_spare_default_case():
    union = lower_field(
        '<union name="body" discrimField="../id">'
        '<command name="a" discrimValue="1"/><spare discrimValue="default" bitCount="30"/>'
        "</union>"
    )

    assert union.cases[1] == Case("default", "spare", DEFAULT, Struct("default", (Spare(30),)))


def test_any_case_can_be_the_default():
    union = lower_field(
        '<union name="body" discrimField="../id">'
        '<command name="other" discrimValue="default"><spare bitCount="4"/></command></union>'
    )

    assert (union.cases[0].name, union.cases[0].value) == ("other", DEFAULT)


def test_spare_case_with_a_value():
    union = lower_field(
        '<union name="body" discrimField="../id"><spare discrimValue="3" bitCount="8"/></union>'
    )

    assert union.cases[0] == Case("spare3", "spare", 3, Struct("spare3", (Spare(8),)))


# --- fieldDef refs (inlined, shared) ----------------------------------------


HEADER_DEF = (
    '<group id="header"><integer name="id" bitCount="16"/>'
    '<integer name="key" bitCount="16"/></group>'
)


def test_field_def_ref_is_inlined_with_use_site_name():
    field = lower_field('<group name="hdr" ref="header"/>', defs=HEADER_DEF)

    assert field == Struct(
        "hdr", (Int("id", 16), Int("key", 16)), meta=Meta(origin=("", "header"))
    )


def test_field_def_is_lowered_once_and_shared():
    a, b = lower_fields(
        '<group name="a" ref="header"/><group name="b" ref="header"/>',
        defs=HEADER_DEF,
    )

    assert a.fields is b.fields


NO_ID = """<icd>
  <fieldDefs>
    <group name="orphan"><spare bitCount="4"/></group>
    <group id="hdr"><spare bitCount="8"/></group>
  </fieldDefs>
  <message name="m"><group name="h" ref="hdr"/></message>
</icd>"""


def test_field_def_without_id_is_skipped_with_a_warning():
    icd = lower_string(NO_ID)

    assert icd.messages[0].body.fields[0].fields == (Spare(8),)
    (issue,) = icd.issues
    assert (issue.severity, issue.code, issue.path) == ("warning", "def-without-id", "line 3")
    assert issue.message == "<group name='orphan'> in fieldDefs has no id, so nothing can ref it; skipped"


def test_lowering_warning_names_the_file_and_line():
    (icd,) = lower_many([NO_ID], names=["real.xml"]).values()

    assert icd.issues[0].path == "real.xml:3"


def test_field_def_can_ref_a_later_field_def():
    field = lower_field(
        '<group name="outer" ref="outer"/>',
        defs='<group id="outer"><group name="inner" ref="inner"/></group>'
        '<group id="inner"><spare bitCount="4"/></group>',
    )

    assert field.fields[0].fields == (Spare(4),)


def test_field_def_ref_cycle_is_an_error():
    with pytest.raises(LoweringError, match="cycle.*a -> b -> a"):
        lower_field(
            '<group name="x" ref="a"/>',
            defs='<group id="a"><group name="b" ref="b"/></group>'
            '<group id="b"><group name="a" ref="a"/></group>',
        )


def test_unresolved_ref_is_an_error():
    with pytest.raises(LoweringError, match="unresolved ref 'nope'"):
        lower_field('<group name="x" ref="nope"/>')


# --- use-site attributes and children merge into the fieldDef ---------------


SPEED_DEF = (
    '<scalar id="speed" bitCount="12" scale="0.5" offset="0">'
    "<label>Speed</label><description>Ground speed</description></scalar>"
)


def test_use_site_attribute_overrides_the_def():
    field = lower_field('<scalar name="v" ref="speed" scale="2"/>', defs=SPEED_DEF)

    assert (field.name, field.bits, field.scale) == ("v", 12, 2.0)


def test_def_attributes_are_kept_when_not_overridden():
    field = lower_field('<scalar name="v" ref="speed" offset="5"/>', defs=SPEED_DEF)

    assert (field.bits, field.scale, field.offset) == (12, 0.5, 5.0)


def test_use_site_label_and_description_override_the_def():
    field = lower_field(
        '<scalar name="v" ref="speed"><label>Air speed</label></scalar>', defs=SPEED_DEF
    )

    assert field.meta.label == "Air speed"
    assert field.meta.description == "Ground speed"


def test_merged_field_still_records_its_origin():
    field = lower_field(
        '<scalar name="v" ref="speed" scale="2"><label>Air speed</label></scalar>',
        defs=SPEED_DEF,
    )

    assert field.meta.origin == ("", "speed")


def test_def_named_values_are_kept_through_a_merge():
    field = lower_field(
        '<integer name="m" ref="mode" signed="false"/>',
        defs='<integer id="mode" bitCount="2"><namedValues>'
        '<namedValue name="ON" value="1"/></namedValues></integer>',
    )

    assert [v.name for v in field.enum.values] == ["ON"]


def test_uses_with_the_same_overrides_share_one_node():
    a, b = lower_fields(
        '<scalar name="a" ref="speed" scale="2"/><scalar name="b" ref="speed" scale="2"/>',
        defs=SPEED_DEF,
    )

    assert replace(a, name="b") == b
    assert a.meta is b.meta


def test_count_value_on_a_ref_makes_an_array_of_the_def():
    _, field = lower_fields(
        '<integer name="n" bitCount="4"/><group name="items" ref="header" countValue="../n"/>',
        defs=HEADER_DEF,
    )

    assert isinstance(field, Array)
    assert (field.name, field.count) == ("items", "../n")
    assert field.element.fields == (Int("id", 16), Int("key", 16))


def test_use_site_tag_wins_over_the_def_tag():
    # Like the attributes: the ref'ing element decides, the def fills in.
    field = lower_field('<integer name="x" ref="speed"/>', defs=SPEED_DEF)

    assert isinstance(field, Int)
    assert (field.name, field.bits) == ("x", 12)
    assert field.meta.origin == ("", "speed")
    assert field.meta.label == "Speed"


def test_same_tag_without_overrides_shares_the_def():
    a, b = lower_fields(
        '<scalar name="a" ref="speed"/><scalar name="b" ref="speed"/>', defs=SPEED_DEF
    )

    assert a.meta is b.meta


def test_ref_with_other_children_is_an_error():
    with pytest.raises(
        LoweringError,
        match=r"<group name='hdr'> refs 'header', so it can only have <label> and "
        r"<description> children, not <integer>",
    ):
        lower_field(
            '<group name="hdr" ref="header"><integer name="extra" bitCount="8"/></group>',
            defs=HEADER_DEF,
        )


def test_ref_cannot_add_named_values_to_a_scalar():
    with pytest.raises(LoweringError, match="<namedValues> only applies to integers"):
        lower_field(
            '<scalar name="v" ref="speed"><namedValues/></scalar>', defs=SPEED_DEF
        )


def test_message_ref_cannot_have_children_either():
    with pytest.raises(LoweringError, match="not <spare>"):
        lower_field(
            '<group name="inner" ref="other"><spare bitCount="2"/></group>',
            extra='<message name="other"/>',
        )


def test_case_ref_cannot_have_children():
    with pytest.raises(LoweringError, match="not <integer>"):
        lower_field(
            '<union name="body" discrimField="id">'
            '<command name="truck" discrimValue="7" ref="other">'
            '<integer name="x" bitCount="8"/></command></union>',
            extra='<message name="other"/>',
        )


# --- message refs (kept as references) --------------------------------------


def test_ref_to_message_name_becomes_message_ref():
    field = lower_field('<group name="inner" ref="other"/>', extra='<message name="other"/>')

    assert field == MessageRef("inner", ("", "other"))


def test_union_case_can_carry_a_message():
    field = lower_field(
        '<union name="body" discrimField="id">'
        '<command name="truck" discrimValue="7" ref="other"/></union>',
        extra='<message name="other"/>',
    )

    assert field.cases[0].body == MessageRef("truck", ("", "other"))


def test_ref_matching_field_def_and_message_is_ambiguous():
    with pytest.raises(LoweringError, match="ambiguous ref 'header'"):
        lower_field(
            '<group name="x" ref="header"/>',
            defs=HEADER_DEF,
            extra='<message name="header"/>',
        )


# --- errors -----------------------------------------------------------------


def test_unknown_element_is_an_error():
    with pytest.raises(LoweringError, match="unknown element <bogus>"):
        lower_field('<bogus name="x"/>')


# --- xsi:type ---------------------------------------------------------------


def test_undeclared_xsi_prefix_is_accepted():
    with pytest.raises(LoweringError, match="unknown xsi:type 'mystery'"):
        lower_field('<group name="g" xsi:type="mystery"/>')


def test_declared_xsi_prefix_is_accepted():
    with pytest.raises(LoweringError, match="unknown xsi:type 'mystery'"):
        lower_string(
            '<icd xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<message name="m"><group name="g" xsi:type="mystery"/></message></icd>'
        )


def test_xsi_type_dispatches_to_dialect_method():
    class Dialect(Lowering):
        def lower_type_mystery(self, elem):
            return Spare(3)

    icd = lower_string(
        '<icd><message name="m"><group name="g" xsi:type="mystery"/></message></icd>',
        lowering=Dialect,
    )

    assert icd.messages[0].body.fields == (Spare(3),)


def test_xsi_type_on_a_ref_is_not_ignored():
    with pytest.raises(LoweringError, match="unknown xsi:type 'mystery'"):
        lower_string(
            '<icd><fieldDefs><group id="d"><spare bitCount="8"/></group></fieldDefs>'
            '<message name="m"><group name="g" ref="d" xsi:type="mystery"/></message></icd>'
        )


# --- namespace: the ICD's own uri and alias ---------------------------------


def test_icd_namespace_and_alias_come_from_namespace_element():
    icd = lower_string(
        '<icd><namespace alias="example">http://example.com/schema</namespace></icd>'
    )

    assert (icd.namespace, icd.alias) == ("http://example.com/schema", "example")


def test_namespace_alias_is_optional():
    icd = lower_string("<icd><namespace>http://example.com/schema</namespace></icd>")

    assert (icd.namespace, icd.alias) == ("http://example.com/schema", "")


def test_import_works_into_an_icd_without_an_alias():
    icds = lower_many(
        [
            '<icd><namespace>urn:main</namespace>'
            '<refSchemaLocation alias="s">urn:support s.xsd</refSchemaLocation>'
            '<message name="m"><group name="x" ref="s:XYZ"/></message></icd>',
            '<icd><namespace>urn:support</namespace>'
            '<fieldDefs><group id="XYZ"><spare bitCount="4"/></group></fieldDefs></icd>',
        ]
    )

    assert icds["urn:main"].messages[0].body.fields[0].fields == (Spare(4),)


def test_icd_without_namespace_element_has_empty_namespace():
    icd = lower_string("<icd/>")

    assert (icd.namespace, icd.alias) == ("", "")


def test_more_than_one_namespace_element_is_an_error():
    with pytest.raises(LoweringError, match="more than one <namespace>"):
        lower_string(
            '<icd><namespace alias="a">urn:a</namespace>'
            '<namespace alias="b">urn:b</namespace></icd>'
        )


SELF = (
    '<icd><namespace alias="me">urn:self</namespace>'
    '<fieldDefs><group id="hdr"><spare bitCount="4"/></group></fieldDefs>'
    '<message name="m">{body}</message><message name="other"/></icd>'
)


def test_own_alias_prefix_on_message_ref_resolves_locally():
    icd = lower_string(SELF.format(body='<group name="x" ref="me:other"/>'))

    assert icd.messages[0].body.fields[0] == MessageRef("x", ("urn:self", "other"))


def test_own_alias_prefix_on_field_def_resolves_locally():
    icd = lower_string(SELF.format(body='<group name="x" ref="me:hdr"/>'))

    assert icd.messages[0].body.fields[0].meta.origin == ("urn:self", "hdr")


def test_unknown_alias_prefix_is_an_error():
    with pytest.raises(LoweringError, match="unknown namespace alias 'nope'"):
        lower_string(SELF.format(body='<group name="x" ref="nope:other"/>'))


# --- several files ----------------------------------------------------------


def test_lower_many_keys_icds_by_declared_namespace():
    icds = lower_many(
        [
            '<icd><namespace alias="a">urn:a</namespace></icd>',
            '<icd><namespace alias="b">urn:b</namespace></icd>',
        ]
    )

    assert sorted(icds) == ["urn:a", "urn:b"]


def test_lower_many_rejects_duplicate_namespace():
    with pytest.raises(LoweringError, match="namespace 'urn:a' is declared by more than one ICD"):
        lower_many(
            [
                '<icd><namespace alias="a">urn:a</namespace></icd>',
                '<icd><namespace alias="a2">urn:a</namespace></icd>',
            ]
        )


# --- <default> --------------------------------------------------------------


def test_integer_default():
    field = lower_field('<integer name="a" bitCount="8"><default>5</default></integer>')

    assert field.default == 5


def test_integer_default_can_be_hex_or_a_named_value():
    hex_ = lower_field('<integer name="a" bitCount="8"><default>0x1F</default></integer>')
    named = lower_field(
        '<integer name="a" bitCount="2"><namedValues><namedValue name="ON" value="1"/>'
        "</namedValues><default>ON</default></integer>"
    )

    assert (hex_.default, named.default) == (0x1F, 1)


def test_scalar_default_is_an_engineering_value():
    field = lower_field(
        '<scalar name="v" bitCount="8" scale="0.5"><default>2.5</default></scalar>'
    )

    assert field.default == 2.5


def test_field_without_default_has_none():
    assert lower_field('<integer name="a" bitCount="8"/>').default is None


def test_default_that_is_not_a_number_or_name_is_an_error():
    with pytest.raises(LoweringError, match="default 'lots' of <integer name='a'> is not a number"):
        lower_field('<integer name="a" bitCount="8"><default>lots</default></integer>')


# --- ref overrides of namedValues and default --------------------------------


MODE_DEF = (
    '<integer id="mode" bitCount="2"><namedValues restrict="true">'
    '<namedValue name="OFF" value="0"/><namedValue name="ON" value="1"/>'
    "</namedValues><default>1</default></integer>"
)


def test_ref_can_override_named_values():
    field = lower_field(
        '<integer name="m" ref="mode"><namedValues><namedValue name="AUTO" value="2"/>'
        "</namedValues></integer>",
        defs=MODE_DEF,
    )

    assert field.enum == Enum((NamedValue("AUTO", 2),), restrict=False)
    assert field.default == 1  # not overridden


def test_ref_can_override_default():
    field = lower_field(
        '<integer name="m" ref="mode"><default>0</default></integer>', defs=MODE_DEF
    )

    assert field.default == 0
    assert [v.name for v in field.enum.values] == ["OFF", "ON"]


def test_ref_default_can_name_a_def_named_value():
    field = lower_field(
        '<integer name="m" ref="mode"><default>OFF</default></integer>', defs=MODE_DEF
    )

    assert field.default == 0


def test_named_values_on_a_ref_to_a_group_is_an_error():
    with pytest.raises(LoweringError, match="<namedValues> only applies to integers"):
        lower_field(
            '<group name="h" ref="header"><namedValues/></group>', defs=HEADER_DEF
        )


# --- namedValues fromUnion ---------------------------------------------------


FROM_UNION = (
    '<integer name="command_id" bitCount="16">'
    '<namedValues fromUnion="CommandBodyUnion" restrict="true"/><default>0</default></integer>'
    '<union name="CommandBodyUnion" discrimField="../command_id">'
    '<command name="power" discrimValue="1"/><command name="ibit" discrimValue="2"/>'
    "</union>"
)


def test_named_values_from_a_union_are_its_cases():
    command_id, _ = lower_fields(FROM_UNION)

    assert command_id.enum == Enum(
        (NamedValue("power", 1), NamedValue("ibit", 2)),
        restrict=True,
        from_union="CommandBodyUnion",
    )
    assert command_id.default == 0


def test_from_union_keeps_listed_named_values_too():
    command_id, _ = lower_fields(
        FROM_UNION.replace(
            'restrict="true"/>', 'restrict="true"><namedValue name="none" value="0"/></namedValues>'
        )
    )

    assert [v.name for v in command_id.enum.values] == ["power", "ibit", "none"]


def test_from_union_finds_the_union_in_an_enclosing_group():
    # The id is in a shared header fieldDef; the union is in the message.
    header, _ = lower_fields(
        '<group name="header" ref="hdr"/>'
        '<union name="Body" discrimField="../header/id">'
        '<command name="a" discrimValue="7"/></union>',
        defs='<group id="hdr"><integer name="id" bitCount="8">'
        '<namedValues fromUnion="Body"/></integer></group>',
    )

    assert header.fields[0].enum.values == (NamedValue("a", 7),)


def test_shared_def_gets_each_messages_own_union():
    icd = lower_string(
        '<icd><fieldDefs><group id="hdr"><integer name="id" bitCount="8">'
        '<namedValues fromUnion="Body"/></integer></group></fieldDefs>'
        + "".join(
            f'<message name="{m}"><group name="header" ref="hdr"/>'
            f'<union name="Body" discrimField="../header/id">'
            f'<command name="{case}" discrimValue="{v}"/></union></message>'
            for m, case, v in (("cmd", "power", 1), ("ret", "status", 9))
        )
        + "</icd>"
    )

    ids = [m.body.fields[0].fields[0].enum.values for m in icd.messages]
    assert ids == [(NamedValue("power", 1),), (NamedValue("status", 9),)]


def test_unknown_from_union_is_a_warning():
    icd = lower_string(
        '<icd><message name="m"><integer name="id" bitCount="8">'
        '<namedValues fromUnion="Nope"/></integer></message></icd>'
    )

    (issue,) = icd.issues
    assert (issue.code, issue.path) == ("from-union-not-found", "m/id")
    assert issue.message == "namedValues fromUnion 'Nope': no union of that name encloses this field"


# --- unions as fieldDefs; fromUnion="#id" ------------------------------------


UNION_DEF = (
    '<union id="CmdUnion">'
    '<command name="power" discrimValue="1"><spare bitCount="8"/></command>'
    '<command name="ibit" discrimValue="2"/>'
    "</union>"
)


def test_union_field_def_needs_no_discrim_field():
    _, body = lower_fields(
        '<integer name="id" bitCount="8"/><union name="body" ref="CmdUnion" discrimField="../id"/>',
        defs=UNION_DEF,
    )

    assert isinstance(body, Union)
    assert (body.name, body.discrim) == ("body", "../id")
    assert [c.name for c in body.cases] == ["power", "ibit"]


def test_union_nested_in_a_group_def_needs_no_discrim_field():
    field = lower_field(
        '<group name="g" ref="g"/>',
        defs='<group id="g"><union name="u"><command name="a" discrimValue="1"/></union></group>',
    )

    assert field.fields[0].discrim == ""


def test_union_in_a_message_still_needs_a_discrim_field():
    with pytest.raises(LoweringError, match="missing required attribute 'discrimField'"):
        lower_field('<union name="body"><command name="a" discrimValue="1"/></union>')


def test_from_union_by_id_uses_the_union_field_def():
    field = lower_field(
        '<integer name="command_id" bitCount="16">'
        '<namedValues fromUnion="#CmdUnion" restrict="true"/></integer>',
        defs=UNION_DEF,
    )

    assert field.enum == Enum(
        (NamedValue("power", 1), NamedValue("ibit", 2)), restrict=True, from_union="#CmdUnion"
    )


def test_from_union_by_id_in_a_shared_def():
    field = lower_field(
        '<group name="header" ref="hdr"/>',
        defs=UNION_DEF + '<group id="hdr"><integer name="id" bitCount="8">'
        '<namedValues fromUnion="#CmdUnion"/></integer></group>',
    )

    assert [v.name for v in field.fields[0].enum.values] == ["power", "ibit"]


def test_from_union_by_unknown_id_is_a_warning():
    icd = lower_string(
        "<icd>\n"
        '<message name="m"><integer name="id" bitCount="8">\n'
        '<namedValues fromUnion="#Nope"/></integer></message></icd>'
    )

    (issue,) = icd.issues
    assert (issue.code, issue.path) == ("from-union-not-found", "line 3")
    assert issue.message == "namedValues fromUnion '#Nope': no fieldDef with id 'Nope'"


def test_from_union_by_id_of_a_non_union_is_a_warning():
    icd = lower_string(
        '<icd><fieldDefs><group id="hdr"><spare bitCount="8"/></group></fieldDefs>'
        '<message name="m"><integer name="id" bitCount="8">'
        '<namedValues fromUnion="#hdr"/></integer></message></icd>'
    )

    assert icd.issues[0].message == "namedValues fromUnion '#hdr': fieldDef 'hdr' is a <group>, not a <union>"


# --- imports: refSchemaLocation ---------------------------------------------


SUPPORT = (
    '<icd><namespace alias="{alias}">https://example.com/support-icd</namespace>'
    '<fieldDefs><group id="XYZ"><spare bitCount="4"/></group></fieldDefs>'
    '<message name="status"/></icd>'
)
IMPORT = (
    '<refSchemaLocation alias="support">'
    "https://example.com/support-icd support-msg.xsd</refSchemaLocation>"
)


def main_icd(body: str, imports: str = IMPORT) -> str:
    return (
        f'<icd><namespace alias="main">urn:main</namespace>{imports}'
        f'<message name="m">{body}</message></icd>'
    )


def test_imported_alias_resolves_to_the_icd_with_that_uri():
    icds = lower_many(
        [main_icd('<group name="x" ref="support:XYZ"/>'), SUPPORT.format(alias="sup")]
    )

    (x,) = icds["urn:main"].messages[0].body.fields
    assert x.fields == (Spare(4),)
    assert x.meta.origin == ("https://example.com/support-icd", "XYZ")


def test_imported_message_becomes_a_message_ref():
    icds = lower_many(
        [main_icd('<group name="s" ref="support:status"/>'), SUPPORT.format(alias="sup")]
    )

    (s,) = icds["urn:main"].messages[0].body.fields
    assert s == MessageRef("s", ("https://example.com/support-icd", "status"))


def test_without_an_import_a_prefix_matches_a_loaded_icds_own_alias():
    icds = lower_many(
        [
            main_icd('<group name="x" ref="support:XYZ"/>', imports=""),
            SUPPORT.format(alias="support"),
        ]
    )

    assert icds["urn:main"].messages[0].body.fields[0].fields == (Spare(4),)


def test_import_order_of_files_does_not_matter():
    icds = lower_many(
        [SUPPORT.format(alias="sup"), main_icd('<group name="x" ref="support:XYZ"/>')]
    )

    assert icds["urn:main"].messages[0].body.fields[0].fields == (Spare(4),)


def test_imported_icd_that_is_not_loaded_is_an_error():
    with pytest.raises(
        LoweringError,
        match="alias 'support' imports 'https://example.com/support-icd', which is not "
        r"loaded \(pass its file too, e.g. support-icd.xml\)",
    ):
        lower_many([main_icd('<group name="x" ref="support:XYZ"/>')])


def test_import_needs_an_alias():
    with pytest.raises(LoweringError, match="missing required attribute 'alias' on <refSchemaLocation>"):
        lower_many([main_icd("", imports="<refSchemaLocation>urn:x x.xsd</refSchemaLocation>")])


def test_import_needs_a_uri():
    with pytest.raises(LoweringError, match="<refSchemaLocation alias='s'> has no namespace uri"):
        lower_many([main_icd("", imports='<refSchemaLocation alias="s"/>')])


# --- error locations --------------------------------------------------------


NESTED_ERROR = """<icd>
  <message name="m">
    <group name="g">
      <scalar name="freq" scale="2"/>
    </group>
  </message>
</icd>"""


def test_error_reports_the_line_of_the_element():
    with pytest.raises(LoweringError) as exc:
        lower_string(NESTED_ERROR)

    assert exc.value.line == 4
    assert str(exc.value) == "line 4: missing required attribute 'bitCount' on <scalar name='freq'>"


def test_error_in_a_field_def_reports_the_line_in_the_def():
    text = (
        "<icd>\n"
        "  <fieldDefs>\n"
        '    <group id="d">\n'
        '      <integer name="x"/>\n'
        "    </group>\n"
        "  </fieldDefs>\n"
        '  <message name="m"><group name="g" ref="d"/></message>\n'
        "</icd>"
    )

    with pytest.raises(LoweringError, match="^line 4: missing required attribute 'bitCount'"):
        lower_string(text)


def test_error_on_a_message_reports_its_line():
    with pytest.raises(LoweringError, match="^line 2: missing required attribute 'name' on <message>"):
        lower_string("<icd>\n  <message/>\n</icd>")


def test_file_name_and_line_together():
    with pytest.raises(LoweringError) as exc:
        lower_many([NESTED_ERROR], names=["real.xml"])

    assert str(exc.value).startswith("real.xml:4: missing required attribute 'bitCount'")


def test_elements_built_without_parse_have_no_line():
    import xml.etree.ElementTree as ET

    root = ET.fromstring('<icd><message name="m"><scalar name="s"/></message></icd>')

    with pytest.raises(LoweringError) as exc:
        lower_many([root])

    assert exc.value.line is None
    assert str(exc.value) == "missing required attribute 'bitCount' on <scalar name='s'>"


# --- fromUnion="//Message/path" ----------------------------------------------


COMMAND_MSG = (
    '<message name="Command"><integer name="CommandNumber" bitCount="8"/>'
    '<union name="CommandBody" discrimField="../CommandNumber">'
    '<command name="power" discrimValue="1"/><command name="ibit" discrimValue="2"/>'
    "</union></message>"
)


def test_from_union_by_absolute_path_in_another_message():
    icd = lower_string(
        "<icd>" + COMMAND_MSG + '<message name="log"><integer name="n" bitCount="8">'
        '<namedValues fromUnion="//Command/CommandBody"/></integer></message></icd>'
    )

    (n,) = icd.messages[1].body.fields
    assert n.enum.values == (NamedValue("power", 1), NamedValue("ibit", 2))
    assert icd.issues == ()


def test_from_union_by_absolute_path_in_its_own_message():
    icd = lower_string(
        "<icd>"
        + COMMAND_MSG.replace(
            '<integer name="CommandNumber" bitCount="8"/>',
            '<integer name="CommandNumber" bitCount="8">'
            '<namedValues fromUnion="//Command/CommandBody"/></integer>',
        )
        + "</icd>"
    )

    number = icd.messages[0].body.fields[0]
    assert [v.name for v in number.enum.values] == ["power", "ibit"]


def test_from_union_by_absolute_path_through_an_import():
    main = (
        '<icd><namespace>urn:main</namespace>'
        '<refSchemaLocation alias="cmds">urn:cmds cmds.xsd</refSchemaLocation>'
        '<message name="log"><integer name="n" bitCount="8">'
        '<namedValues fromUnion="//cmds:Command/CommandBody"/></integer></message></icd>'
    )
    cmds = "<icd><namespace>urn:cmds</namespace>" + COMMAND_MSG + "</icd>"

    icds = lower_many([main, cmds])

    (n,) = icds["urn:main"].messages[0].body.fields
    assert [v.name for v in n.enum.values] == ["power", "ibit"]


@pytest.mark.parametrize(
    "path, message",
    [
        ("//Nope/CommandBody", "no message 'Nope'"),
        ("//Command/Missing", "no field 'Missing' in message 'Command'"),
        ("//Command/CommandNumber", "'CommandNumber' in message 'Command' is not a union"),
    ],
)
def test_bad_absolute_from_union_is_a_warning(path, message):
    icd = lower_string(
        "<icd>" + COMMAND_MSG + '<message name="log"><integer name="n" bitCount="8">'
        f'<namedValues fromUnion="{path}"/></integer></message></icd>'
    )

    (issue,) = icd.issues
    assert (issue.code, issue.path) == ("from-union-not-found", "log/n")
    assert issue.message == f"namedValues fromUnion {path!r}: {message}"


# --- <ieeeFloat> --------------------------------------------------------------


@pytest.mark.parametrize("bits", [32, 64])
def test_ieee_float_is_32_or_64_bits(bits):
    field = lower_field(f'<ieeeFloat name="temp" bitCount="{bits}"><label>Temp</label></ieeeFloat>')

    assert field == Float("temp", bits, meta=Meta(label="Temp"))


@pytest.mark.parametrize("bits", ["16", "48", "variable"])
def test_ieee_float_of_another_size_is_an_error(bits):
    with pytest.raises(LoweringError, match=r"<ieeeFloat name='temp'>: bitCount must be 32 or 64"):
        lower_field(f'<ieeeFloat name="temp" bitCount="{bits}"/>')


def test_ieee_float_needs_a_bit_count():
    with pytest.raises(LoweringError, match="missing required attribute 'bitCount'"):
        lower_field('<ieeeFloat name="temp"/>')


def test_ieee_float_default():
    assert lower_field('<ieeeFloat name="t" bitCount="32"><default>1.5</default></ieeeFloat>').default == 1.5


def test_ieee_float_ref_can_override_the_default():
    field = lower_field(
        '<ieeeFloat name="t" ref="f"><default>-2</default></ieeeFloat>',
        defs='<ieeeFloat id="f" bitCount="64"><default>1</default></ieeeFloat>',
    )

    assert (field.bits, field.default) == (64, -2.0)


def test_ieee_float_ref_cannot_override_its_size_to_another_one():
    with pytest.raises(LoweringError, match="bitCount must be 32 or 64"):
        lower_field(
            '<ieeeFloat name="t" ref="f" bitCount="16"/>', defs='<ieeeFloat id="f" bitCount="32"/>'
        )


# --- <hexdata> ----------------------------------------------------------------


def test_hexdata_with_a_bit_count_is_fixed_bytes():
    field = lower_field('<hexdata name="raw" bitCount="64"><label>Raw</label></hexdata>')

    assert field == Bytes("raw", 64, meta=Meta(label="Raw"))


def test_variable_hexdata_counts_bytes_with_count_variable():
    _, field = lower_fields(
        '<integer name="n" bitCount="8"/>'
        '<hexdata name="raw" bitCount="variable" countVariable="../n"/>'
    )

    assert field == Bytes("raw", VARIABLE, count="../n")


@pytest.mark.parametrize(
    "xml, error",
    [
        ('<hexdata name="raw" bitCount="12"/>', "bitCount 12 is not a whole number of bytes"),
        ('<hexdata name="raw"/>', "missing required attribute 'bitCount'"),
        (
            '<hexdata name="raw" bitCount="variable"/>',
            "missing required attribute 'countVariable'",
        ),
        (
            '<hexdata name="raw" bitCount="16" countVariable="../n"/>',
            "countVariable only applies when bitCount is variable",
        ),
    ],
)
def test_bad_hexdata_is_an_error(xml, error):
    with pytest.raises(LoweringError, match=error):
        lower_field(xml)


def test_hexdata_can_be_a_field_def():
    field = lower_field('<hexdata name="raw" ref="blob"/>', defs='<hexdata id="blob" bitCount="32"/>')

    assert (field.name, field.bits) == ("raw", 32)


# --- a spare case without discrimValue ---------------------------------------


def spare_union(cases: str):
    return lower_string(
        '<icd><message name="m"><integer name="id" bitCount="8"/>'
        f'<union name="body" discrimField="../id">{cases}</union></message></icd>'
    )


def test_lone_spare_without_discrim_value_is_the_default_case_with_a_warning():
    icd = spare_union('<command name="a" discrimValue="1"/><spare bitCount="8"/>')

    union = icd.messages[0].body.fields[1]
    assert [(c.name, c.value) for c in union.cases] == [("a", 1), ("default", DEFAULT)]
    (issue,) = icd.issues
    assert (issue.severity, issue.code, issue.path) == ("warning", "spare-without-discrim-value", "line 1")
    assert issue.message == (
        '<spare> in union \'body\' has no discrimValue; treated as discrimValue="default"'
    )


def test_spare_without_discrim_value_next_to_another_spare_is_an_error():
    with pytest.raises(LoweringError, match="missing required attribute 'discrimValue' on <spare>"):
        spare_union('<spare discrimValue="1" bitCount="8"/><spare bitCount="8"/>')


# --- nestContents -------------------------------------------------------------


def test_groups_and_unions_nest_by_default():
    group, union = lower_fields(
        '<group name="g"><integer name="x" bitCount="8"/></group>'
        '<union name="u" discrimField="../g/x"><spare discrimValue="default" bitCount="8"/></union>'
    )

    assert group.nest and union.nest


def test_nest_contents_false():
    group, union = lower_fields(
        '<group name="g" nestContents="false"><integer name="x" bitCount="8"/></group>'
        '<union name="u" discrimField="../g/x" nestContents="false">'
        '<spare discrimValue="default" bitCount="8"/></union>'
    )

    assert not group.nest and not union.nest


def test_nest_contents_can_be_set_where_a_group_def_is_used():
    field = lower_field(
        '<group name="h" ref="hdr" nestContents="false"/>',
        defs='<group id="hdr"><integer name="x" bitCount="8"/></group>',
    )

    assert not field.nest


def test_nest_contents_must_be_true_or_false():
    with pytest.raises(LoweringError, match="nestContents must be true or false"):
        lower_fields('<group name="g" nestContents="no"><integer name="x" bitCount="8"/></group>')


def test_an_array_always_nests():
    with pytest.raises(LoweringError, match="nestContents.*array"):
        lower_fields(
            '<integer name="n" bitCount="8"/>'
            '<group name="g" countValue="../n" nestContents="false"><integer name="x" bitCount="8"/></group>'
        )
