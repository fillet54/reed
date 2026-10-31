"""Lowering one element at a time: XML snippet in, IR node out."""

import pytest

from reed.ir import (
    VARIABLE,
    Array,
    Case,
    Enum,
    Int,
    Message,
    MessageRef,
    Meta,
    NamedValue,
    Scaled,
    Spare,
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


# --- spare ------------------------------------------------------------------


def test_spare():
    assert lower_field('<spare bitCount="10"/>') == Spare(10)


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


# --- namespace: the ICD's own uri and alias ---------------------------------


def test_icd_namespace_and_alias_come_from_namespace_element():
    icd = lower_string(
        '<icd><namespace alias="example">http://example.com/schema</namespace></icd>'
    )

    assert (icd.namespace, icd.alias) == ("http://example.com/schema", "example")


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
