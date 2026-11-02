"""Diffs between two versions of an ICD, variant by variant."""

import json
import re
from pathlib import Path

from reed.__main__ import main
from reed.dialect import Dialect
from reed.diff import diff, leaves, summary_lines


def icd(body: str, extra: str = "") -> str:
    return f'<icd><message name="m">{body}</message>{extra}</icd>'


def diffed(old: str, new: str, *patterns: str):
    """The diff of message "m" between two bodies: its tree, keyed by variant name."""
    dialect = Dialect("test", expand=patterns)
    tree = diff(dialect.lower([icd(old)]), dialect.lower([icd(new)]), dialect)
    return {v["name"]: v for v in leaves(tree)}


def only(old: str, new: str):
    (variant,) = diffed(old, new).values()
    return variant


def whats(variant):
    return [(c["path"], c["what"], c["category"]) for c in variant["changes"]]


A = '<integer name="a" bitCount="8"/>'
B = '<integer name="b" bitCount="8"/>'
SPARE8 = '<spare bitCount="8"/>'


# --- categories -------------------------------------------------------------


def test_the_same_icd_has_no_changes():
    v = only(A + B, A + B)

    assert v["status"] == "unchanged"
    assert v["changes"] == []
    assert all(r["change"] is None for r in v["rows"])


def test_label_and_description_changes_are_editorial():
    v = only(
        '<integer name="a" bitCount="16"><description>old</description></integer>',
        '<integer name="a" bitCount="16"><label>A</label><description>new</description></integer>',
    )

    assert v["status"] == "editorial"
    assert whats(v) == [("a", "docs", "editorial")]
    (row,) = v["rows"]
    assert row["change"] == "changed"
    assert row["was"] == {"label": None, "description": "old"}


def test_a_field_in_spare_bits_is_compatible():
    v = only(A + SPARE8, A + '<integer name="flag" bitCount="1"/><spare bitCount="7"/>')

    assert v["status"] == "compatible"
    assert whats(v) == [("flag", "added", "compatible")]
    assert "spare" in v["changes"][0]["text"]


def test_a_field_appended_to_the_message_is_compatible():
    v = only(A + B, A + B + '<integer name="c" bitCount="16"/>')

    assert v["status"] == "compatible"
    assert whats(v) == [("c", "added", "compatible")]
    assert "end" in v["changes"][0]["text"]


def test_a_field_inserted_in_the_middle_moves_the_rest():
    v = only(A + B, A + '<integer name="x" bitCount="16"/>' + B)

    assert v["status"] == "incompatible"
    assert whats(v) == [("x", "added", "incompatible"), ("b", "moved", "incompatible")]


def test_resized_removed_and_retyped_fields_are_incompatible():
    assert whats(only(A + B, A + '<integer name="b" bitCount="4"/><spare bitCount="4"/>')) == [
        ("b", "resized", "incompatible")
    ]
    assert whats(only(A + B, A + SPARE8)) == [("b", "removed", "incompatible")]
    assert whats(only(A + B, A + '<integer name="b" bitCount="8" signed="true"/>')) == [
        ("b", "type", "incompatible")
    ]


def test_a_renamed_field_is_compatible():
    v = only(A + B, A + '<integer name="bee" bitCount="8"/>')

    assert whats(v) == [("bee", "renamed", "compatible")]
    assert v["rows"][1]["was"]["name"] == "b"


def test_named_values_added_are_compatible_and_removed_are_not():
    def ints(*values):
        named = "".join(f'<namedValue name="{n}" value="{x}"/>' for n, x in values)
        return f'<integer name="a" bitCount="16"><namedValues>{named}</namedValues></integer>'

    assert whats(only(ints(("off", 0)), ints(("off", 0), ("on", 1)))) == [
        ("a", "values", "compatible")
    ]
    assert whats(only(ints(("off", 0), ("on", 1)), ints(("off", 0)))) == [
        ("a", "values", "incompatible")
    ]


def test_a_shorter_message_is_incompatible():
    v = only(A + B + '<spare bitCount="16"/>', A + B)

    assert v["status"] == "incompatible"
    assert [c["what"] for c in v["changes"]] == ["size"]


def test_the_worst_change_decides():
    v = only(
        '<integer name="a" bitCount="8"><description>x</description></integer>' + B,
        '<integer name="a" bitCount="8"><description>y</description></integer>' + B + A.replace('"a"', '"c"'),
    )

    assert v["status"] == "compatible"


# --- arrays and unions --------------------------------------------------------


def test_array_element_spare_is_usable_but_growing_the_element_is_not():
    def arr(fields):
        return f'<integer name="n" bitCount="16"/><group name="xs" countValue="../n">{fields}</group>'

    used = only(arr(A + SPARE8), arr(A + B))
    grown = only(arr(A + B), arr(A + B + '<integer name="c" bitCount="16"/>'))

    assert whats(used) == [("xs/b", "added", "compatible")]
    assert grown["status"] == "incompatible"


UNION = (
    '<integer name="id" bitCount="16"/>'
    '<union name="body" discrimField="../id">'
    '<group name="one" discrimValue="1">' + A + SPARE8 + "</group>"
    "{more}</union>"
)


def test_variants_are_diffed_one_by_one():
    old = UNION.format(more='<group name="two" discrimValue="2">' + A + B + "</group>")
    new = UNION.format(
        more='<group name="two" discrimValue="2">' + A + "</group>"
        '<group name="three" discrimValue="3">' + A + "</group>"
    ).replace(SPARE8, B, 1)
    variants = diffed(old, new, "m/id")

    assert {name: v["status"] for name, v in variants.items()} == {
        "m/one": "compatible",
        "m/two": "incompatible",
        "m/three": "compatible",
    }
    assert whats(variants["m/three"]) == [("", "variant added", "compatible")]


def test_a_removed_variant_is_incompatible():
    old = UNION.format(more='<group name="two" discrimValue="2">' + A + "</group>")
    variants = diffed(old, UNION.format(more=""), "m/id")

    assert variants["m/two"]["status"] == "incompatible"
    assert whats(variants["m/two"]) == [("", "variant removed", "incompatible")]


def test_a_changed_case_value_is_incompatible():
    old = UNION.format(more="")
    new = old.replace('discrimValue="1"', 'discrimValue="9"')

    assert diffed(old, new, "m/id")["m/one"]["status"] == "incompatible"
    assert ("body/one", "type", "incompatible") in whats(only(old, new))


def test_a_new_case_of_an_unsplit_union_is_compatible():
    old = UNION.format(more="")
    new = UNION.format(more='<group name="two" discrimValue="2">' + A + "</group>")

    assert ("body/two", "added", "compatible") in whats(only(old, new))


# --- the merged rows -----------------------------------------------------------


def test_removed_rows_sit_where_they_were():
    v = only(A + B + '<integer name="c" bitCount="16"/>', A + '<spare bitCount="8"/><integer name="c" bitCount="16"/>')

    assert [(r["name"], r["change"]) for r in v["rows"]] == [
        ("a", None),
        ("b", "removed"),
        ("(spare)", "added"),
        ("c", None),
    ]


def test_moved_rows_keep_their_old_place():
    v = only(A + B, '<integer name="b" bitCount="8"/>' + A)

    b = next(r for r in v["rows"] if r["name"] == "b")
    assert b["change"] == "changed"
    assert b["was"]["offset"] == 8 and b["offset"] == 0
    assert b["was"]["bit_span"] == "7–0"


def test_summary_lines_count_each_category():
    lines = summary_lines(
        diff(
            *(Dialect("test").lower([icd(body)]) for body in (A + B, A + B + A.replace('"a"', '"c"'))),
            Dialect("test"),
        )
    )

    assert lines[0] == "1 compatible"
    assert any("m" in line and "compatible" in line for line in lines[1:])


# --- reed diff ----------------------------------------------------------------


def embedded(html: str) -> dict:
    blob = re.search(r'<script id="icd-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(blob.group(1))


def test_cli_writes_the_diff_page(tmp_path, capsys):
    new = tmp_path / "example-icd.xml"
    text = Path("tests/data/example-icd.xml").read_text()
    new.write_text(
        text.replace(
            '<spare bitCount="10" />\n      </command>',
            '<integer name="level" bitCount="2" />\n        <spare bitCount="8" />\n      </command>',
            1,
        )
    )
    for other in Path("tests/data").glob("*.xml"):  # its imports
        if other.name != new.name:
            (tmp_path / other.name).write_text(other.read_text())
    out = tmp_path / "diff.html"

    code = main(["diff", "--dialect", "example", "-o", str(out), "tests/data/example-icd.xml", str(new)])

    assert code == 0
    printed = capsys.readouterr().out
    assert "example:Command/power" in printed and "compatible" in printed
    data = embedded(out.read_text())
    assert data["diff"]["counts"]["compatible"] == 1
    power = next(v for v in leaves_of(data) if v["key"] == "example:Command/power")
    assert power["status"] == "compatible"


def leaves_of(data):
    def walk(v):
        return [x for c in v["children"] for x in walk(c)] if v["children"] else [v]

    return [x for icd in data["icds"] for m in icd["messages"] for x in walk(m)]


def test_a_variants_own_docs_are_editorial():
    old = UNION.format(more="")
    new = old.replace('<group name="one" discrimValue="1">', '<group name="one" discrimValue="1"><label>One</label>')

    assert whats(diffed(old, new, "m/id")["m/one"]) == [("", "docs", "editorial")]


def test_named_values_of_a_fixed_field_dont_matter_to_the_variant():
    def union(more):
        return (
            '<integer name="id" bitCount="16"><namedValues fromUnion="body"/></integer>'
            '<union name="body" discrimField="../id">'
            f'<group name="one" discrimValue="1">{A}{SPARE8}</group>{more}</union>'
        )

    variants = diffed(union(""), union(f'<group name="two" discrimValue="2">{A}</group>'), "m/id")

    assert variants["m/one"]["status"] == "unchanged"


def test_change_texts_leave_out_unnested_containers():
    old = '<group name="g" nestContents="false">' + A + B + "</group>"
    new = '<group name="g" nestContents="false">' + A + "</group>" + SPARE8

    (change,) = only(old, new)["changes"]
    assert change["path"] == "g/b"
    assert change["text"].startswith("b: removed")
