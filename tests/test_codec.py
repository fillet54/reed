"""Codec: Python dicts ⇄ packed binary messages."""

import pytest

from reed.codec import CodecError, decode, encode
from reed.dialect import Dialect
from reed.dialects.example import EXAMPLE
from reed.ir import LayoutPolicy


def message(body: str, defs: str = "", extra: str = "", **layout):
    """Message "m", lowered by a dialect with the given layout options."""
    dialect = Dialect("test", layout=LayoutPolicy(**layout))
    icds = dialect.lower(
        [f"<icd><fieldDefs>{defs}</fieldDefs><message name=\"m\">{body}</message>{extra}</icd>"]
    )
    return icds[""].messages[0], icds


def roundtrip(body: str, value: dict, **kwargs) -> bytes:
    msg, icds = message(body, **kwargs)
    data = encode(msg, value, icds)
    assert decode(msg, data, icds) == value
    return data


# --- packing ----------------------------------------------------------------


def test_fields_pack_msb_first():
    data = roundtrip(
        '<integer name="a" bitCount="4"/><integer name="b" bitCount="12"/>',
        {"a": 0xA, "b": 0x123},
    )

    assert data == bytes.fromhex("a123")


def test_message_is_padded_to_a_whole_word():
    assert roundtrip('<integer name="a" bitCount="8"/>', {"a": 0xAB}) == bytes.fromhex("ab00")


def test_fields_can_cross_word_boundaries():
    data = roundtrip(
        '<integer name="a" bitCount="12"/><integer name="b" bitCount="8"/>'
        '<integer name="c" bitCount="12"/>',
        {"a": 0xABC, "b": 0xDE, "c": 0xF01},
    )

    assert data == bytes.fromhex("abcdef01")


def test_little_endian_words():
    msg, icds = message(
        '<integer name="a" bitCount="16"/><integer name="b" bitCount="16"/>', byteorder="little"
    )

    data = encode(msg, {"a": 0x1234, "b": 0x5678}, icds)

    assert data == bytes.fromhex("34127856")
    assert decode(msg, data, icds) == {"a": 0x1234, "b": 0x5678}


def test_without_padding_a_message_must_be_whole_words():
    msg, icds = message('<integer name="a" bitCount="8"/>', pad=False)

    with pytest.raises(CodecError, match="m: 8 bits is not a whole number of 16-bit words"):
        encode(msg, {"a": 1}, icds)

    with pytest.raises(CodecError, match="m: 8 bits left over"):
        decode(msg, bytes.fromhex("0100"), icds)


def test_without_padding_a_carried_message_packs_tightly():
    msg, icds = message(
        '<group name="p" ref="payload"/><integer name="after" bitCount="12"/>',
        extra='<message name="payload"><integer name="a" bitCount="4"/></message>',
        pad=False,
    )

    data = encode(msg, {"p": {"a": 0xF}, "after": 0xABC}, icds)

    assert data == bytes.fromhex("fabc")
    assert decode(msg, data, icds) == {"p": {"a": 0xF}, "after": 0xABC}


# --- integers ---------------------------------------------------------------


def test_signed_integer_is_twos_complement():
    data = roundtrip('<integer name="a" bitCount="8" signed="true"/>', {"a": -1})

    assert data == bytes.fromhex("ff00")


def test_value_too_big_for_its_bits_is_an_error():
    msg, icds = message('<integer name="a" bitCount="8"/>')

    with pytest.raises(CodecError, match="m/a: 256 does not fit in 8 unsigned bits"):
        encode(msg, {"a": 256}, icds)


def test_signed_value_out_of_range_is_an_error():
    msg, icds = message('<integer name="a" bitCount="8" signed="true"/>')

    with pytest.raises(CodecError, match="m/a: 128 does not fit in 8 signed bits"):
        encode(msg, {"a": 128}, icds)


MODE = (
    '<integer name="mode" bitCount="2"><namedValues{restrict}>'
    '<namedValue name="OFF" value="0"/><namedValue name="ON" value="1"/>'
    "</namedValues></integer>"
)


def test_named_values_encode_from_names_and_decode_to_names():
    data = roundtrip(MODE.format(restrict=""), {"mode": "ON"})

    assert data == bytes.fromhex("4000")


def test_numbers_are_accepted_for_named_values():
    msg, icds = message(MODE.format(restrict=""))

    assert decode(msg, encode(msg, {"mode": 1}, icds), icds) == {"mode": "ON"}


def test_unnamed_value_of_unrestricted_enum_decodes_to_a_number():
    assert roundtrip(MODE.format(restrict=""), {"mode": 3}) == bytes.fromhex("c000")


def test_unknown_name_is_an_error():
    msg, icds = message(MODE.format(restrict=""))

    with pytest.raises(CodecError, match="m/mode: 'BRIGHT' is not a named value"):
        encode(msg, {"mode": "BRIGHT"}, icds)


def test_restricted_enum_rejects_other_numbers():
    msg, icds = message(MODE.format(restrict=' restrict="true"'))

    with pytest.raises(CodecError, match="m/mode: 3 is not one of the named values"):
        encode(msg, {"mode": 3}, icds)

    with pytest.raises(CodecError, match="m/mode: 3 is not one of the named values"):
        decode(msg, bytes.fromhex("c000"), icds)


# --- strings ----------------------------------------------------------------


def test_fixed_string_is_padded_with_zeros():
    data = roundtrip('<string name="s" bitCount="48"/>', {"s": "hi"})

    assert data == b"hi\0\0\0\0"


def test_fixed_string_that_fills_the_field_has_no_terminator():
    assert roundtrip('<string name="s" bitCount="32"/>', {"s": "abcd"}) == b"abcd"


def test_fixed_string_too_long_is_an_error():
    msg, icds = message('<string name="s" bitCount="32"/>')

    with pytest.raises(CodecError, match="m/s: 'abcde' is 5 characters; the field holds 4"):
        encode(msg, {"s": "abcde"}, icds)


def test_variable_string_is_a_length_byte_then_characters():
    assert roundtrip('<string name="s" bitCount="variable"/>', {"s": "abc"}) == b"\x03abc"


def test_variable_string_is_not_aligned():
    data = roundtrip(
        '<string name="s" bitCount="variable"/><integer name="a" bitCount="8"/>',
        {"s": "a", "a": 0xFF},
    )

    assert data == b"\x01a\xff\x00"  # the last byte is the message's implicit spare


def test_empty_variable_string():
    assert roundtrip('<string name="s" bitCount="variable"/>', {"s": ""}) == b"\x00\x00"


def test_variable_string_longer_than_255_is_an_error():
    msg, icds = message('<string name="s" bitCount="variable"/>')

    with pytest.raises(CodecError, match="m/s: 256 characters; a variable string holds at most 255"):
        encode(msg, {"s": "x" * 256}, icds)


def test_string_value_must_be_a_str():
    msg, icds = message('<string name="s" bitCount="32"/>')

    with pytest.raises(CodecError, match="m/s: expected a string, got 5"):
        encode(msg, {"s": 5}, icds)


def test_character_that_is_not_one_byte_is_an_error():
    msg, icds = message('<string name="s" bitCount="32"/>')

    with pytest.raises(CodecError, match="m/s: 'π' is not a one-byte character"):
        encode(msg, {"s": "π"}, icds)


# --- defaults ---------------------------------------------------------------


def test_missing_field_uses_its_default():
    msg, icds = message(
        '<integer name="a" bitCount="8"><default>7</default></integer>'
        '<integer name="b" bitCount="8"/>'
    )

    assert encode(msg, {"b": 1}, icds) == bytes.fromhex("0701")


def test_given_value_beats_the_default():
    msg, icds = message('<integer name="a" bitCount="16"><default>7</default></integer>')

    assert encode(msg, {"a": 9}, icds) == bytes.fromhex("0009")


def test_scalar_default_is_scaled():
    msg, icds = message(
        '<scalar name="v" bitCount="16" scale="0.5" offset="10"><default>12.5</default></scalar>'
    )

    assert encode(msg, {}, icds) == bytes.fromhex("0005")


def test_default_need_not_be_a_restricted_value():
    # e.g. command_id defaults to 0 while the cases start at 1
    msg, icds = message(MODE.format(restrict=' restrict="true"').replace(
        "</namedValues>", "</namedValues><default>3</default>"
    ))

    assert encode(msg, {}, icds) == bytes.fromhex("c000")


def test_chosen_case_beats_the_discriminator_default():
    msg, icds = message(UNION.replace(
        '<integer name="id" bitCount="8"/>',
        '<integer name="id" bitCount="8"><default>0</default></integer>',
    ))

    assert encode(msg, {"body": {"small": {"x": 5}}}, icds) == bytes.fromhex("0105")


# --- namedValues fromUnion ---------------------------------------------------


FROM_UNION = (
    '<integer name="command_id" bitCount="8">'
    '<namedValues fromUnion="Body" restrict="true"/></integer>'
    '<union name="Body" discrimField="../command_id">'
    '<command name="power" discrimValue="1"><integer name="x" bitCount="8"/></command>'
    "</union>"
)


def test_from_union_discriminator_decodes_to_the_case_name():
    msg, icds = message(FROM_UNION)

    data = encode(msg, {"Body": {"power": {"x": 3}}}, icds)

    assert decode(msg, data, icds) == {"command_id": "power", "Body": {"power": {"x": 3}}}


def test_from_union_name_is_accepted_when_encoding():
    msg, icds = message(FROM_UNION)

    assert encode(msg, {"command_id": "power", "Body": {"power": {"x": 3}}}, icds) == bytes.fromhex("0103")


# --- scalars and spares -----------------------------------------------------


def test_scalar_is_raw_times_scale_plus_offset():
    data = roundtrip(
        '<scalar name="v" bitCount="8" scale="0.5" offset="10"/>', {"v": 12.5}
    )

    assert data == bytes.fromhex("0500")  # raw 5


def test_spares_are_zero_even_when_named():
    msg, icds = message('<spare name="pad" bitCount="8"/><integer name="a" bitCount="8"/>')

    assert encode(msg, {"a": 0xFF}, icds) == bytes.fromhex("00ff")


def test_spare_bits_are_ignored_when_decoding():
    msg, icds = message('<spare bitCount="8"/><integer name="a" bitCount="8"/>')

    assert decode(msg, bytes.fromhex("abff"), icds) == {"a": 0xFF}


def test_spares_are_zero_and_not_in_the_value():
    data = roundtrip('<spare bitCount="4"/><integer name="a" bitCount="4"/>', {"a": 0xF})

    assert data == bytes.fromhex("0f00")


# --- groups -----------------------------------------------------------------


def test_groups_are_nested_dicts():
    roundtrip(
        '<group name="g"><integer name="a" bitCount="8"/><integer name="b" bitCount="8"/></group>',
        {"g": {"a": 1, "b": 2}},
    )


def test_missing_field_is_an_error():
    msg, icds = message('<group name="g"><integer name="a" bitCount="8"/></group>')

    with pytest.raises(CodecError, match="m/g/a: missing"):
        encode(msg, {"g": {}}, icds)


def test_unknown_field_is_an_error():
    msg, icds = message('<integer name="a" bitCount="8"/>')

    with pytest.raises(CodecError, match="m: unknown field 'b'"):
        encode(msg, {"a": 1, "b": 2}, icds)


# --- unions -----------------------------------------------------------------


UNION = (
    '<integer name="id" bitCount="8"/>'
    '<union name="body" discrimField="../id">'
    '<command name="small" discrimValue="1"><integer name="x" bitCount="8"/></command>'
    '<command name="big" discrimValue="2"><integer name="y" bitCount="24"/></command>'
    "</union>"
)


def test_union_case_sets_its_discriminator():
    msg, icds = message(UNION)

    data = encode(msg, {"body": {"big": {"y": 0xABCDEF}}}, icds)

    assert data == bytes.fromhex("02abcdef")
    assert decode(msg, data, icds) == {"id": 2, "body": {"big": {"y": 0xABCDEF}}}


def test_matching_explicit_discriminator_is_fine():
    roundtrip(UNION, {"id": 1, "body": {"small": {"x": 5}}})


def test_conflicting_discriminator_is_an_error():
    msg, icds = message(UNION)

    with pytest.raises(CodecError, match="m/id: is 1 but case 'big' needs 2"):
        encode(msg, {"id": 1, "body": {"big": {"y": 0}}}, icds)


def test_union_value_must_name_one_case():
    msg, icds = message(UNION)

    with pytest.raises(CodecError, match="m/body: expected one of small, big"):
        encode(msg, {"body": {"small": {"x": 1}, "big": {"y": 1}}}, icds)


def test_decoding_an_unknown_discriminator_is_an_error():
    msg, icds = message(UNION)

    with pytest.raises(CodecError, match="m/body: no case for discriminator value 9"):
        decode(msg, bytes.fromhex("0900"), icds)


# --- default cases ----------------------------------------------------------


DEFAULT_UNION = UNION.replace(
    "</union>", '<spare discrimValue="default" bitCount="8"/></union>'
)


def test_unknown_discriminator_decodes_as_the_default_case():
    msg, icds = message(DEFAULT_UNION)

    assert decode(msg, bytes.fromhex("0900"), icds) == {"id": 9, "body": {"default": {}}}


def test_default_case_encodes_with_a_given_discriminator():
    msg, icds = message(DEFAULT_UNION)

    assert encode(msg, {"id": 9, "body": {"default": {}}}, icds) == bytes.fromhex("0900")


def test_default_case_needs_a_discriminator_value():
    msg, icds = message(DEFAULT_UNION)

    with pytest.raises(
        CodecError, match="m/id: missing; case 'default' is the default case, so give a value"
    ):
        encode(msg, {"body": {"default": {}}}, icds)


def test_default_case_uses_the_discriminator_field_default():
    msg, icds = message(DEFAULT_UNION.replace(
        '<integer name="id" bitCount="8"/>',
        '<integer name="id" bitCount="8"><default>0</default></integer>',
    ))

    assert encode(msg, {"body": {"default": {}}}, icds) == bytes.fromhex("0000")


def test_default_case_cannot_use_another_cases_value():
    msg, icds = message(DEFAULT_UNION)

    with pytest.raises(CodecError, match="m/id: 2 belongs to case 'big', not the default case"):
        encode(msg, {"id": 2, "body": {"default": {}}}, icds)


def test_union_without_a_discrim_field_is_an_error():
    msg, icds = message(
        '<union name="body" ref="U"/>',
        defs='<union id="U"><command name="a" discrimValue="1"/></union>',
    )

    with pytest.raises(CodecError, match="m/body: union has no discrimField"):
        encode(msg, {"body": {"a": {}}}, icds)
    with pytest.raises(CodecError, match="m/body: union has no discrimField"):
        decode(msg, bytes.fromhex("0000"), icds)


# --- arrays -----------------------------------------------------------------


ARRAY = (
    '<integer name="n" bitCount="4"/>'
    '<group name="items" countValue="../n"><integer name="v" bitCount="4"/></group>'
)


def test_array_is_a_list_and_sets_its_count():
    msg, icds = message(ARRAY)

    data = encode(msg, {"items": [{"v": 1}, {"v": 2}, {"v": 3}]}, icds)

    assert data == bytes.fromhex("3123")
    assert decode(msg, data, icds) == {"n": 3, "items": [{"v": 1}, {"v": 2}, {"v": 3}]}


def test_empty_array():
    assert roundtrip(ARRAY, {"n": 0, "items": []}) == bytes.fromhex("0000")


def test_array_longer_than_its_count_allows_is_an_error():
    msg, icds = message(ARRAY)

    with pytest.raises(CodecError, match="m/n: 16 does not fit in 4 unsigned bits"):
        encode(msg, {"items": [{"v": 0}] * 16}, icds)


# --- unions keyed by a system ID (example dialect) --------------------------


SYSTEM_STATUS = """
<icd><fieldDefs>
  <group id="SubsystemType" xsi:type="subsystemDef" bitCount="21">
    <system name="fuel" value="10"><subsystem name="tanks" value="1"/></system>
  </group>
</fieldDefs>
<message name="status">
  <group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>
  <union discrimField="../systemId" xsi:type="subsystemRef">
    <system name="fuel" value="10">
      <integer name="total" bitCount="8"/>
      <subsystem name="tanks" value="1"><integer name="leak" bitCount="3"/></subsystem>
    </system>
  </union>
</message></icd>
"""


def test_system_id_union_fills_the_system_id():
    icds = EXAMPLE.lower([SYSTEM_STATUS])
    (msg,) = icds[""].messages

    data = encode(msg, {"fuel.tanks": {"leak": 5}}, icds)
    value = decode(msg, data, icds)

    assert value == {
        "systemId": {"system": 10, "subsystem": 1, "unit": 0},
        "fuel.tanks": {"leak": 5},
    }
    assert int.from_bytes(data, "big") >> (32 - 24) == (10 << 17) | (1 << 10) | (5 << 0)


def test_system_id_without_a_case_does_not_decode():
    icds = EXAMPLE.lower([SYSTEM_STATUS])
    (msg,) = icds[""].messages
    data = ((10 << 17) | (2 << 10)).to_bytes(3, "big") + b"\0"  # fuel, subsystem 2

    with pytest.raises(CodecError, match=r"no case for discriminator value \(10, 2, 0\)"):
        decode(msg, data, icds)


# --- message refs -----------------------------------------------------------


def test_carried_message_is_a_nested_dict():
    data = roundtrip(
        '<integer name="id" bitCount="8"/>'
        '<union name="body" discrimField="../id">'
        '<command name="truck" discrimValue="7" ref="payload"/></union>',
        {"id": 7, "body": {"truck": {"a": 1, "b": 2}}},
        extra='<message name="payload"><integer name="a" bitCount="4"/>'
        '<integer name="b" bitCount="4"/></message>',
    )

    # The carried message is padded to its own word: 07 | 12 00 | pad to a word.
    assert data == bytes.fromhex("07120000")


def test_fields_after_a_carried_message_start_on_a_word():
    data = roundtrip(
        '<group name="p" ref="payload"/><integer name="after" bitCount="16"/>',
        {"p": {"a": 0xF}, "after": 0xABCD},
        extra='<message name="payload"><integer name="a" bitCount="4"/></message>',
    )

    assert data == bytes.fromhex("f000abcd")


def test_word_size_comes_from_the_dialect_layout():
    msg, icds = message('<integer name="a" bitCount="8"/>', word_bits=32)

    data = encode(msg, {"a": 0xAB}, icds)

    assert data == bytes.fromhex("ab000000")
    assert decode(msg, data, icds) == {"a": 0xAB}


# --- data length ------------------------------------------------------------


def test_short_data_is_an_error():
    msg, icds = message('<integer name="a" bitCount="32"/>')

    with pytest.raises(CodecError, match="m/a: message ends after 16 bits"):
        decode(msg, bytes.fromhex("0001"), icds)


def test_trailing_words_are_an_error():
    msg, icds = message('<integer name="a" bitCount="16"/>')

    with pytest.raises(CodecError, match="m: 16 bits left over"):
        decode(msg, bytes.fromhex("00010002"), icds)


def test_data_must_be_whole_words():
    msg, icds = message('<integer name="a" bitCount="8"/>')

    with pytest.raises(CodecError, match="m: 3 bytes is not a whole number of 16-bit words"):
        decode(msg, bytes.fromhex("000000"), icds)


# --- the example ICD --------------------------------------------------------


def example():
    icds = EXAMPLE.load(["tests/data/example-icd.xml"])  # and test-icd.xml, by import
    messages = {m.name: m for icd in icds.values() for m in icd.messages}
    return messages, icds


RCS_QUAD_B = {"system": 3, "subsystem": 1, "unit": 2}


def test_example_power_command():
    messages, icds = example()
    value = {
        "header": {"key": 7, "time": 360000},
        "power": {"systemId": RCS_QUAD_B, "mode": "ON"},  # CommandBody doesn't nest
    }

    data = encode(messages["Command"], value, icds)

    # CommandNumber=1, key=7, time=360000 (01:00:00 UTC), then systemId=(3,1,2)
    # in 21 bits, mode=1 and 10 spare bits
    assert data.hex() == "0001" "0007" "00057e40" "06041400"
    assert decode(messages["Command"], data, icds) == {
        "header": {"CommandNumber": "power", "key": 7, "time": 360000},
        "power": {"systemId": RCS_QUAD_B, "mode": "ON"},
    }


def test_example_test_command_carries_a_test_icd_message():
    messages, icds = example()
    value = {
        "header": {"key": 1, "time": 0},
        "TestCommand": {
            "TestCommandBody": {  # the carried message; its own TestCommandBody union doesn't nest
                "header": {"TestNumber": "memoryCheck"},
                "memoryCheck": {"startAddress": 0o4000, "wordCount": 64},
            }
        },
    }

    data = encode(messages["Command"], value, icds)

    assert data.hex() == "0005" "0001" "00000000" "0003" "0800" "0040"
    assert decode(messages["Command"], data, icds) == {
        **value,
        "header": {"CommandNumber": "TestCommand", "key": 1, "time": 0},
    }


def test_example_stored_command():
    messages, icds = example()
    value = {
        "header": {"key": 2, "time": 0},
        "storeAp": {
            "executeAt": 100,
            "stored": {"gimbalTrim": {"pitch": 1.5, "yaw": -0.5}},
        },
    }

    data = encode(messages["Command"], value, icds)
    stored = decode(messages["Command"], data, icds)["storeAp"]

    assert data.hex() == "0004" "0002" "00000000" "0002" "00000064" "3fc00000" "bf000000"
    assert stored == {
        "CommandNumber": "gimbalTrim",  # filled in from the stored command
        "executeAt": 100,
        "stored": {"gimbalTrim": {"pitch": 1.5, "yaw": -0.5}},
    }


# --- floats -----------------------------------------------------------------


def test_float32_is_its_ieee_bits():
    data = roundtrip('<ieeeFloat name="f" bitCount="32"/>', {"f": 1.5})

    assert data == bytes.fromhex("3fc00000")


def test_float64_is_its_ieee_bits():
    data = roundtrip('<ieeeFloat name="f" bitCount="64"/>', {"f": -0.1})

    assert data == bytes.fromhex("bfb999999999999a")


def test_float32_rounds_to_single_precision():
    msg, icds = message('<ieeeFloat name="f" bitCount="32"/>')

    assert decode(msg, encode(msg, {"f": 0.1}, icds), icds) == {"f": pytest.approx(0.1)}


def test_float_accepts_an_int():
    assert roundtrip('<ieeeFloat name="f" bitCount="32"/>', {"f": 2}) == bytes.fromhex("40000000")


def test_float_words_follow_the_byte_order():
    data = roundtrip('<ieeeFloat name="f" bitCount="32"/>', {"f": 1.5}, byteorder="little")

    assert data == bytes.fromhex("c03f0000")


def test_missing_float_uses_its_default():
    msg, icds = message('<ieeeFloat name="f" bitCount="32"><default>1.5</default></ieeeFloat>')

    assert encode(msg, {}, icds) == bytes.fromhex("3fc00000")


@pytest.mark.parametrize("value, error", [("x", "expected a number"), (True, "expected a number")])
def test_float_must_be_a_number(value, error):
    msg, icds = message('<ieeeFloat name="f" bitCount="32"/>')

    with pytest.raises(CodecError, match=f"m/f: {error}"):
        encode(msg, {"f": value}, icds)


def test_float_too_big_for_32_bits_is_an_error():
    msg, icds = message('<ieeeFloat name="f" bitCount="32"/>')

    with pytest.raises(CodecError, match=r"m/f: 1e\+300 does not fit in a 32-bit float"):
        encode(msg, {"f": 1e300}, icds)


# --- hexdata ----------------------------------------------------------------


def test_fixed_hexdata_is_its_bytes():
    data = roundtrip('<hexdata name="raw" bitCount="32"/>', {"raw": b"\xde\xad\xbe\xef"})

    assert data == bytes.fromhex("deadbeef")


def test_fixed_hexdata_accepts_a_bytearray():
    msg, icds = message('<hexdata name="raw" bitCount="16"/>')

    assert encode(msg, {"raw": bytearray(b"\x01\x02")}, icds) == bytes.fromhex("0102")


def test_fixed_hexdata_must_be_its_exact_size():
    msg, icds = message('<hexdata name="raw" bitCount="32"/>')

    with pytest.raises(CodecError, match="m/raw: 2 bytes; the field holds 4"):
        encode(msg, {"raw": b"\x01\x02"}, icds)


@pytest.mark.parametrize("value", ["dead", [1, 2], None])
def test_hexdata_must_be_bytes(value):
    msg, icds = message('<hexdata name="raw" bitCount="16"/>')

    with pytest.raises(CodecError, match="m/raw: expected bytes"):
        encode(msg, {"raw": value}, icds)


def test_missing_hexdata_is_an_error():
    msg, icds = message('<hexdata name="raw" bitCount="16"/>')

    with pytest.raises(CodecError, match="m/raw: missing"):
        encode(msg, {}, icds)


VARIABLE_HEX = (
    '<integer name="n" bitCount="8"/>'
    '<hexdata name="raw" bitCount="variable" countVariable="../n"/>'
)


def test_variable_hexdata_fills_in_its_byte_count():
    msg, icds = message(VARIABLE_HEX)

    data = encode(msg, {"raw": b"\xaa\xbb\xcc"}, icds)

    assert data == bytes.fromhex("03aabbcc")
    assert decode(msg, data, icds) == {"n": 3, "raw": b"\xaa\xbb\xcc"}


def test_variable_hexdata_count_must_agree():
    msg, icds = message(VARIABLE_HEX)

    with pytest.raises(CodecError, match="m/n: is 2 but 3 bytes needs 3"):
        encode(msg, {"n": 2, "raw": b"\xaa\xbb\xcc"}, icds)


def test_empty_variable_hexdata():
    assert roundtrip(VARIABLE_HEX, {"n": 0, "raw": b""}) == bytes.fromhex("0000")


# --- nestContents -------------------------------------------------------------


def test_an_unnested_group_puts_its_fields_in_the_enclosing_dict():
    data = roundtrip(
        '<integer name="a" bitCount="8"/>'
        '<group name="g" nestContents="false"><integer name="b" bitCount="8"/></group>',
        {"a": 1, "b": 2},
    )

    assert data == bytes.fromhex("0102")


def test_an_unnested_union_puts_its_case_in_the_enclosing_dict():
    body = (
        '<integer name="id" bitCount="8"/>'
        '<union name="body" discrimField="../id" nestContents="false">'
        '<command name="a" discrimValue="1"><integer name="x" bitCount="8"/></command>'
        '<command name="b" discrimValue="2"><integer name="y" bitCount="8"/></command>'
        "</union>"
    )

    assert roundtrip(body, {"id": 2, "b": {"y": 7}}) == bytes.fromhex("0207")


def test_unnested_contents_nest_inside_named_groups():
    roundtrip(
        '<group name="outer"><group name="inner" nestContents="false">'
        '<integer name="x" bitCount="16"/></group></group>',
        {"outer": {"x": 5}},
    )


def test_an_unnested_group_name_is_not_a_key():
    msg, icds = message('<group name="g" nestContents="false"><integer name="b" bitCount="16"/></group>')

    with pytest.raises(CodecError, match="unknown field 'g'"):
        encode(msg, {"g": {"b": 1}}, icds)


def test_example_command_body_is_not_nested():
    icds = EXAMPLE.load(["tests/data/example-icd.xml"])
    command = next(m for m in icds["http://example.com/example-icd"].messages if m.name == "Command")
    value = {
        "header": {"CommandNumber": "spsBurn", "key": 9, "time": 100},
        "spsBurn": {"ignitionTime": 5, "deltaV": 1.5, "burnDuration": 2.0},
    }

    assert decode(command, encode(command, value, icds), icds) == value
