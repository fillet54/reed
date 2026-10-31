"""Validation: rules run over the IR and report every issue with its path."""

from reed.ir import Struct
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
