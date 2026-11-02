"""Validation: rules run over the IR and report every issue with its path."""

import pytest

from reed.dialect import Dialect
from reed.ir import LayoutPolicy, Struct
from reed.lower import lower_string
from reed.validate import BASE_RULES, rule, validate


def issues(body: str, defs: str = "", rules=BASE_RULES):
    icd = lower_string(
        f"<icd><fieldDefs>{defs}</fieldDefs><message name=\"m\">{body}</message></icd>"
    )
    return [(i.severity, i.code, i.path) for i in validate(icd, rules=rules)]


def test_valid_message_has_no_issues():
    assert issues('<group name="g" bitCount="8"><spare bitCount="8"/></group>') == []


# --- declared bitCount ------------------------------------------------------


def test_declared_bit_count_must_match_fields():
    assert issues('<group name="g" bitCount="16"><spare bitCount="8"/></group>') == [
        ("error", "bit-count", "m/g")
    ]


def test_declared_bit_count_checked_on_field_defs_at_use_site():
    assert issues(
        '<group name="use" ref="d"/>',
        defs='<group id="d" bitCount="4"><spare bitCount="8"/></group>',
    ) == [("error", "bit-count", "m/use")]


def test_variable_bit_count_that_is_fixed_is_a_warning():
    assert issues('<group name="g" bitCount="variable"><spare bitCount="8"/></group>') == [
        ("warning", "variable-is-fixed", "m/g")
    ]


def test_variable_bit_count_that_varies_is_fine():
    assert (
        issues(
            '<integer name="n" bitCount="2"/>'
            '<group name="g" bitCount="variable">'
            '<group name="items" countValue="../../n"><spare bitCount="8"/></group></group>'
        )
        == []
    )


# --- message size limit -----------------------------------------------------


def test_message_over_max_size_is_an_error():
    assert issues('<spare bitCount="4097"/>') == [("error", "message-too-large", "m")]


def test_message_that_may_exceed_max_size_is_a_warning():
    assert issues(
        '<integer name="n" bitCount="16"/>'
        '<group name="items" countValue="../n"><spare bitCount="8"/></group>'
    ) == [("warning", "message-may-exceed", "m")]


# --- discriminators ---------------------------------------------------------


def test_union_discriminator_resolves():
    assert (
        issues(
            '<group name="hdr"><integer name="id" bitCount="8"/></group>'
            '<union name="body" discrimField="../hdr/id">'
            '<command name="a" discrimValue="1"/></union>'
        )
        == []
    )


def test_unresolved_union_discriminator_is_an_error():
    assert issues(
        '<union name="body" discrimField="../nope"><command name="a" discrimValue="1"/></union>'
    ) == [("error", "unresolved-path", "m/body")]


def test_discriminator_after_its_union_is_an_error():
    assert issues(
        '<union name="body" discrimField="../id"><command name="a" discrimValue="1"/></union>'
        '<integer name="id" bitCount="8"/>'
    ) == [("error", "used-before-defined", "m/body")]


def test_duplicate_case_values_are_an_error():
    assert issues(
        '<integer name="id" bitCount="8"/>'
        '<union name="body" discrimField="../id">'
        '<command name="a" discrimValue="1"/><command name="b" discrimValue="1"/></union>'
    ) == [("error", "duplicate-case-value", "m/body")]


# --- array counts -----------------------------------------------------------


def test_unresolved_count_is_an_error():
    assert ("error", "unresolved-path", "m/items") in issues(
        '<group name="items" countValue="nope"><spare bitCount="8"/></group>'
    )


def test_count_resolves_from_the_array_like_discrim_field():
    # Without "../", "n" would have to be inside the array itself.
    assert issues(
        '<integer name="n" bitCount="4"/>'
        '<group name="items" countValue="n"><spare bitCount="8"/></group>'
    ) == [("error", "unresolved-path", "m/items")]


def test_count_must_be_an_integer():
    assert issues(
        '<group name="n"><spare bitCount="4"/></group>'
        '<group name="items" countValue="../n"><spare bitCount="8"/></group>'
    ) == [("error", "count-not-integer", "m/items")]


def test_count_after_its_array_is_an_error():
    assert issues(
        '<group name="items" countValue="../n"><spare bitCount="8"/></group>'
        '<integer name="n" bitCount="4"/>'
    ) == [("error", "used-before-defined", "m/items")]


# --- custom rules -----------------------------------------------------------


def test_extra_rules_run_alongside_base_rules():
    @rule(Struct)
    def no_empty_groups(node, ctx):
        if not node.fields:
            yield ctx.error("empty-group", f"group {node.name!r} has no fields")

    assert issues(
        '<group name="g"/><group name="h" bitCount="1"/>',
        rules=BASE_RULES + (no_empty_groups,),
    ) == [
        ("error", "empty-group", "m/g"),
        ("error", "bit-count", "m/h"),
        ("error", "empty-group", "m/h"),
    ]


# --- default cases ----------------------------------------------------------


def test_two_default_cases_is_an_error():
    assert issues(
        '<integer name="id" bitCount="8"/><union name="body" discrimField="../id">'
        '<spare discrimValue="default" bitCount="8"/>'
        '<command name="b" discrimValue="default"/></union>'
    ) == [("error", "duplicate-case-value", "m/body")]


# --- strings ----------------------------------------------------------------


def test_fixed_string_must_be_whole_words():
    assert issues('<string name="s" bitCount="24"/>') == [("error", "string-bits", "m/s")]


def test_string_of_whole_words_is_fine():
    assert issues('<string name="s" bitCount="32"/>') == []


def test_variable_string_needs_no_alignment():
    assert issues('<string name="s" bitCount="variable"/><spare bitCount="8"/>') == []


# --- unions without a discriminator ------------------------------------------


def test_union_def_used_without_a_discrim_field_is_an_error():
    assert issues(
        '<union name="body" ref="U"/>',
        defs='<union id="U"><command name="a" discrimValue="1"/></union>',
    ) == [("error", "missing-discrim", "m/body")]


# --- whole words ------------------------------------------------------------


def layout_issues(body: str, **layout):
    dialect = Dialect("test", layout=LayoutPolicy(**layout))
    icds = dialect.lower([f'<icd><message name="m">{body}</message></icd>'])
    return [(i.severity, i.code, i.path) for i in dialect.validate(icds)]


def test_without_padding_a_message_must_be_whole_words():
    assert layout_issues('<spare bitCount="8"/>', pad=False) == [
        ("error", "not-whole-words", "m")
    ]
    assert layout_issues('<spare bitCount="32"/>', pad=False) == []


COUNTED = (
    '<integer name="n" bitCount="{n}"/>'
    '<group name="g" countValue="../n"><spare bitCount="{e}"/></group>'
)


@pytest.mark.parametrize("n, e, ok", [(8, 8, False), (8, 16, False), (16, 16, True)])
def test_without_padding_a_variable_message_must_always_be_whole_words(n, e, ok):
    issues = [i for i in layout_issues(COUNTED.format(n=n, e=e), pad=False) if i[1] == "not-whole-words"]

    assert issues == ([] if ok else [("error", "not-whole-words", "m")])


def test_padding_fills_any_size():
    assert layout_issues('<spare bitCount="8"/>') == []


# --- ref tag mismatch -------------------------------------------------------


SPEED = '<scalar id="speed" bitCount="12" scale="0.5"/>'


def test_ref_with_a_different_tag_warns():
    assert issues('<integer name="x" ref="speed"/>', defs=SPEED) == [
        ("warning", "ref-tag-mismatch", "m/x")
    ]


def test_ref_tag_mismatch_says_which_tag_wins_and_where():
    text = (
        "<icd>\n"
        "  <fieldDefs>\n"
        f"    {SPEED}\n"
        "  </fieldDefs>\n"
        '  <message name="m">\n'
        '    <integer name="x" ref="speed"/>\n'
        "  </message>\n"
        "</icd>"
    )

    (issue,) = validate(lower_string(text))

    assert issue.message == (
        "line 6: <integer name='x'> refs 'speed', which is a <scalar> (line 3); "
        "the <integer> wins"
    )


def test_ref_with_the_same_tag_does_not_warn():
    assert issues('<scalar name="x" ref="speed"/>', defs=SPEED) == []


def test_every_mismatched_use_is_reported():
    assert issues(
        '<integer name="a" ref="speed"/><integer name="b" ref="speed"/>', defs=SPEED
    ) == [
        ("warning", "ref-tag-mismatch", "m/a"),
        ("warning", "ref-tag-mismatch", "m/b"),
    ]


def test_mismatched_ref_used_as_an_array_warns():
    found = issues(
        '<integer name="n" bitCount="4"/><integer name="xs" ref="speed" countValue="../n"/>',
        defs=SPEED,
    )

    assert ("warning", "ref-tag-mismatch", "m/xs") in found


def test_dialect_alias_tags_do_not_warn():
    from reed.dialects.example import EXAMPLE

    icds = EXAMPLE.lower(
        [
            '<icd><fieldDefs><group id="pair"><spare bitCount="16"/></group></fieldDefs>'
            '<message name="m"><group2 name="p" ref="pair"/></message></icd>'
        ]
    )

    assert EXAMPLE.validate(icds) == []


# --- defaults ---------------------------------------------------------------


def test_default_that_does_not_fit_is_an_error():
    assert issues('<integer name="a" bitCount="4"><default>16</default></integer>') == [
        ("error", "default-out-of-range", "m/a")
    ]


def test_scalar_default_that_does_not_fit_is_an_error():
    assert issues(
        '<scalar name="v" bitCount="4" scale="1"><default>-1</default></scalar>'
    ) == [("error", "default-out-of-range", "m/v")]


def test_default_that_fits_is_fine():
    assert issues('<integer name="a" bitCount="4" signed="true"><default>-8</default></integer>') == []


# --- floats -----------------------------------------------------------------


def test_float_default_too_big_for_32_bits_is_an_error():
    assert issues('<ieeeFloat name="f" bitCount="32"><default>1e300</default></ieeeFloat>') == [
        ("error", "default-out-of-range", "m/f")
    ]


def test_float_default_that_fits_is_fine():
    assert issues('<ieeeFloat name="f" bitCount="64"><default>1e300</default></ieeeFloat>') == []


def test_float_ref_tag_mismatch_is_a_warning():
    assert issues(
        '<ieeeFloat name="f" ref="s"/>', defs='<scalar id="s" bitCount="32"/>'
    ) == [("warning", "ref-tag-mismatch", "m/f")]


# --- hexdata ----------------------------------------------------------------


def test_hexdata_count_variable_must_resolve():
    assert issues('<hexdata name="raw" bitCount="variable" countVariable="../nope"/>') == [
        ("error", "unresolved-path", "m/raw")
    ]


def test_hexdata_count_variable_must_be_an_integer():
    assert issues(
        '<group name="g"><spare bitCount="8"/></group>'
        '<hexdata name="raw" bitCount="variable" countVariable="../g"/>'
    ) == [("error", "count-not-integer", "m/raw")]


def test_hexdata_count_variable_must_come_first():
    assert issues(
        '<hexdata name="raw" bitCount="variable" countVariable="../n"/>'
        '<integer name="n" bitCount="8"/>'
    ) == [("error", "used-before-defined", "m/raw")]


# --- nestContents -------------------------------------------------------------


def test_unnested_names_that_clash_are_an_error():
    found = issues(
        '<integer name="x" bitCount="8"/>'
        '<group name="g" nestContents="false"><integer name="x" bitCount="8"/></group>'
    )

    assert ("error", "duplicate-value-key", "m") in found


def test_unnested_case_names_that_clash_are_an_error():
    found = issues(
        '<integer name="a" bitCount="8"/>'
        '<union name="u" discrimField="../a" nestContents="false">'
        '<command name="a" discrimValue="1"><integer name="x" bitCount="8"/></command></union>'
    )

    assert ("error", "duplicate-value-key", "m") in found


def test_unnested_names_that_do_not_clash_are_fine():
    assert issues(
        '<integer name="x" bitCount="8"/>'
        '<group name="g" nestContents="false"><integer name="y" bitCount="8"/></group>'
    ) == []
