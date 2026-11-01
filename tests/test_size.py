"""Size analysis: how many bits a message takes, as a range plus a formula."""

import math

from reed.dialect import Dialect
from reed.ir import LayoutPolicy
from reed.lower import lower_many, lower_string
from reed.size import Linear, Size, size_of


def message_size(body: str, defs: str = "", extra: str = "", padded=False, **kwargs) -> Size:
    """Size of message "m". Unpadded by default: most tests are about the fields."""
    icd = lower_string(
        f"<icd><fieldDefs>{defs}</fieldDefs>"
        f'<message name="m">{body}</message>{extra}</icd>'
    )
    return size_of(icd.messages[0], icds={icd.namespace: icd}, padded=padded, **kwargs)


# --- fixed sizes ------------------------------------------------------------


def test_leaf_fields_add_up():
    size = message_size(
        '<integer name="a" bitCount="4"/><scalar name="b" bitCount="8"/><spare bitCount="4"/>'
    )

    assert size == Size.fixed(16)
    assert size.is_fixed


def test_empty_message_is_zero_bits():
    assert message_size("") == Size.fixed(0)


def test_nested_groups_add_up():
    size = message_size(
        '<group name="g"><integer name="a" bitCount="4"/>'
        '<group name="h"><spare bitCount="12"/></group></group>'
    )

    assert size == Size.fixed(16)


def test_inlined_field_def_counts_at_every_use():
    size = message_size(
        '<group name="a" ref="pair"/><group name="b" ref="pair"/>',
        defs='<group id="pair"><spare bitCount="8"/></group>',
    )

    assert size == Size.fixed(16)


# --- unions -----------------------------------------------------------------


UNION = (
    '<integer name="id" bitCount="8"/>'
    '<union name="body" discrimField="../id">'
    '<command name="small" discrimValue="1"><spare bitCount="8"/></command>'
    '<command name="big" discrimValue="2"><spare bitCount="24"/></command>'
    "</union>"
)


def test_union_ranges_from_smallest_to_largest_case():
    size = message_size(UNION)

    assert (size.lo, size.hi) == (16, 32)
    assert size.linear is None


def test_union_of_equal_cases_is_fixed():
    size = message_size(
        '<union name="body" discrimField="../id">'
        '<command name="a" discrimValue="1"><spare bitCount="8"/></command>'
        '<command name="b" discrimValue="2"><integer name="x" bitCount="8"/></command>'
        "</union>"
    )

    assert size == Size.fixed(8)


def test_choosing_a_case_fixes_the_union():
    icd = lower_string(f'<icd><message name="m">{UNION}</message></icd>')
    message = icd.messages[0]
    union = message.body.fields[1]

    assert size_of(message, choose={id(union): union.cases[1]}) == Size.fixed(32)


# --- arrays -----------------------------------------------------------------


ARRAY = (
    '<integer name="n" bitCount="4"/>'
    '<group name="items" countValue="../n"><integer name="v" bitCount="8"/></group>'
)


def test_array_size_is_linear_in_its_count():
    size = message_size(ARRAY)

    assert size == Size(4, 4 + 15 * 8, Linear(4, (("n", 8),)))


def test_array_count_range_uses_restricted_named_values():
    size = message_size(
        '<integer name="n" bitCount="4"><namedValues restrict="true">'
        '<namedValue name="ONE" value="1"/><namedValue name="THREE" value="3"/>'
        "</namedValues></integer>"
        '<group name="items" countValue="../n"><integer name="v" bitCount="8"/></group>'
    )

    assert (size.lo, size.hi) == (4 + 8, 4 + 24)


def test_array_count_can_be_in_an_enclosing_group():
    size = message_size(
        '<group name="hdr"><integer name="n" bitCount="2"/></group>'
        '<group name="body"><group name="items" countValue="../../hdr/n">'
        '<spare bitCount="8"/></group></group>'
    )

    assert size == Size(2, 2 + 3 * 8, Linear(2, (("hdr/n", 8),)))


def test_array_with_unresolved_count_is_unbounded():
    size = message_size('<group name="items" countValue="nope"><spare bitCount="8"/></group>')

    assert (size.lo, size.hi, size.linear) == (0, math.inf, None)


# --- message refs -----------------------------------------------------------


def test_message_ref_has_the_size_of_its_target():
    size = message_size(
        '<spare bitCount="8"/><group name="inner" ref="other"/>',
        extra='<message name="other"><spare bitCount="16"/></message>',
    )

    assert size == Size.fixed(24)


def test_carried_message_is_padded_to_a_whole_word():
    # A carried message is a message: its 4 bits take a whole 16-bit word.
    size = message_size(
        '<spare bitCount="8"/><group name="inner" ref="other"/>',
        extra='<message name="other"><spare bitCount="4"/></message>',
    )

    assert size == Size.fixed(24)


def test_message_ref_into_another_icd_uses_icds():
    icds = lower_many(
        [
            '<icd><namespace alias="a">urn:a</namespace>'
            '<message name="m"><group name="x" ref="a:other"/></message>'
            '<message name="other"><spare bitCount="16"/></message></icd>'
        ]
    )

    assert size_of(icds["urn:a"].messages[0], icds=icds) == Size.fixed(16)


def test_default_case_counts_in_the_union_size():
    size = message_size(
        '<integer name="id" bitCount="8"/><union name="body" discrimField="../id">'
        '<command name="a" discrimValue="1"><spare bitCount="8"/></command>'
        '<spare discrimValue="default" bitCount="30"/></union>'
    )

    assert (size.lo, size.hi) == (16, 38)


def test_fixed_string_size():
    assert message_size('<string name="s" bitCount="64"/>') == Size.fixed(64)


def test_variable_string_is_a_length_byte_and_up_to_255_characters():
    size = message_size('<string name="s" bitCount="variable"/>')

    assert (size.lo, size.hi) == (8, 8 + 255 * 8)


# --- word padding -----------------------------------------------------------


def test_message_is_padded_to_a_whole_word():
    body = '<integer name="a" bitCount="53"/>'

    assert message_size(body, padded=True) == Size.fixed(64)
    assert message_size(body) == Size.fixed(53)


def test_padding_is_the_default():
    icd = lower_string('<icd><message name="m"><spare bitCount="1"/></message></icd>')

    assert size_of(icd.messages[0]) == Size.fixed(16)


def test_padding_rounds_up_both_bounds():
    size = message_size(ARRAY, padded=True)  # 4 + 8·n, n up to 15

    assert (size.lo, size.hi) == (16, 128)
    assert size.linear is None  # rounding up isn't linear


def test_padding_keeps_a_formula_made_of_whole_words():
    body = (
        '<integer name="n" bitCount="16"/>'
        '<group name="items" countValue="../n"><spare bitCount="32"/></group>'
    )

    assert message_size(body, padded=True) == message_size(body)


def test_unbounded_stays_unbounded():
    size = message_size(
        '<group name="items" countValue="nope"><spare bitCount="8"/></group>', padded=True
    )

    assert size.hi == math.inf


def dialect_size(layout: LayoutPolicy, body: str, extra: str = "") -> Size:
    icds = Dialect("test", layout=layout).lower(
        [f'<icd><message name="m">{body}</message>{extra}</icd>']
    )
    return size_of(icds[""].messages[0], icds)


def test_word_size_comes_from_the_dialect_layout():
    assert dialect_size(LayoutPolicy(word_bits=32), '<spare bitCount="8"/>') == Size.fixed(32)


def test_no_implicit_spare_without_padding():
    layout = LayoutPolicy(pad=False)

    assert dialect_size(layout, '<spare bitCount="8"/>') == Size.fixed(8)
    assert dialect_size(
        layout,
        '<spare bitCount="8"/><group name="inner" ref="other"/>',
        extra='<message name="other"><spare bitCount="4"/></message>',
    ) == Size.fixed(12)


# --- display ----------------------------------------------------------------


def test_str_of_fixed_size():
    assert str(Size.fixed(48)) == "48 bits"


def test_str_of_linear_size():
    assert str(message_size(ARRAY)) == "4 + 8·n bits (4–124)"


def test_str_of_range():
    assert str(message_size(UNION)) == "16–32 bits"


def test_float_size_is_its_bit_count():
    size = message_size('<ieeeFloat name="a" bitCount="32"/><ieeeFloat name="b" bitCount="64"/>')

    assert size == Size.fixed(96)


def test_fixed_hexdata_size_is_its_bit_count():
    assert message_size('<hexdata name="raw" bitCount="48"/>') == Size.fixed(48)


def test_variable_hexdata_size_is_eight_bits_per_count():
    size = message_size(
        '<integer name="n" bitCount="4"/>'
        '<hexdata name="raw" bitCount="variable" countVariable="../n"/>'
    )

    assert (size.lo, size.hi) == (4, 4 + 15 * 8)
    assert size.linear == Linear(4, (("n", 8),))
