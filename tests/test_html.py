"""The HTML viewer: field rows with offsets, and the page around them."""

import json
import re

from reed.__main__ import main
from reed.dialect import Dialect
from reed.dialects.example import EXAMPLE
from reed.expand import expand
from reed.html import field_rows, page


def rows_of(body: str, *patterns: str, extra: str = "", variant: str | None = None):
    """Rows of message "m", or of one of its variants."""
    dialect = Dialect("test", expand=patterns)
    icds = dialect.lower([f'<icd><message name="m">{body}</message>{extra}</icd>'])
    (root, *_) = expand(icds, dialect)[""]
    chosen = root
    if variant:
        chosen = next(v for v in walk(root) if v.name == variant)
    return field_rows(chosen.message, icds)


def walk(variant):
    yield variant
    for child in variant.children:
        yield from walk(child)


def brief(rows, *keys):
    return [tuple(r[k] for k in keys) for r in rows]


# --- offsets ----------------------------------------------------------------


def test_leaves_get_offsets_and_the_last_word_its_implicit_spare():
    rows = rows_of(
        '<integer name="a" bitCount="4"/><integer name="b" bitCount="12"/>'
        '<integer name="c" bitCount="8"/>'
    )

    assert brief(rows, "name", "offset", "bits", "kind") == [
        ("a", 0, 4, "integer"),
        ("b", 4, 12, "integer"),
        ("c", 16, 8, "integer"),
        ("(pad)", 24, 8, "pad"),
    ]


def test_group_fields_are_nested_under_it():
    rows = rows_of(
        '<group name="g"><integer name="x" bitCount="8"/><integer name="y" bitCount="8"/></group>'
    )

    assert brief(rows, "depth", "name", "offset", "bits") == [
        (0, "g", 0, 16),
        (1, "x", 0, 8),
        (1, "y", 8, 8),
    ]
    assert rows[1]["path"] == "g/x"


def test_types_are_described():
    rows = rows_of(
        '<integer name="i" bitCount="8" signed="true"/>'
        '<scalar name="s" bitCount="8" scale="0.5" offset="1"/>'
        '<ieeeFloat name="f" bitCount="32"/>'
        '<string name="t" bitCount="32"/>'
        '<hexdata name="h" bitCount="16"/>'
        '<spare bitCount="8"/>'
    )

    assert brief(rows, "name", "type") == [
        ("i", "int8"),
        ("s", "scaled uint8 × 0.5 + 1"),
        ("f", "float32"),
        ("t", "string, 4 chars"),
        ("h", "hexdata, 2 bytes"),
        ("(spare)", "spare"),
        ("(pad)", "implicit spare"),
    ]


def test_named_values_default_and_docs_are_kept():
    (row, _) = rows_of(
        '<integer name="mode" bitCount="1"><label>Mode</label><description>On or off</description>'
        '<namedValues restrict="true"><namedValue name="OFF" value="0"/>'
        '<namedValue name="ON" value="1"/></namedValues><default>OFF</default></integer>'
    )

    assert row["values"] == [["OFF", 0], ["ON", 1]]
    assert row["restrict"] is True
    assert row["default"] == 0
    assert (row["label"], row["description"]) == ("Mode", "On or off")


def test_offsets_after_a_variable_field_are_unknown():
    rows = rows_of(
        '<string name="t" bitCount="variable"/><integer name="x" bitCount="16"/>'
    )

    assert brief(rows, "name", "offset", "bits", "size") == [
        ("t", 0, None, "8–2048 bits"),
        ("x", None, 16, "16 bits"),
    ]  # no pad row: the padding isn't known


def test_rows_name_their_word_and_bits_msb_first():
    rows = rows_of(
        '<integer name="a" bitCount="4"/><integer name="b" bitCount="12"/>'
        '<integer name="c" bitCount="1"/><spare bitCount="15"/>'
        '<integer name="d" bitCount="32"/><integer name="e" bitCount="8"/>'
        '<integer name="f" bitCount="16"/>'
    )

    assert brief(rows, "name", "word", "bit_span") == [
        ("a", "0", "15–12"),
        ("b", "0", "11–0"),
        ("c", "1", "15"),
        ("(spare)", "1", "14–0"),
        ("d", "2–3", "15–0"),
        ("e", "4", "15–8"),
        ("f", "4–5", "7–8"),
        ("(pad)", "5", "7–0"),
    ]


def test_word_and_bits_of_unknown_and_relative_offsets():
    rows = rows_of(
        '<integer name="n" bitCount="16"/>'
        '<group name="items" countValue="../n"><integer name="a" bitCount="8"/></group>'
        '<integer name="x" bitCount="16"/>'
    )

    assert brief(rows, "name", "word", "bit_span") == [
        ("n", "0", "15–0"),
        ("items", "1–…", "15–…"),  # variable size: the end isn't known
        ("a", "+0", "15–8"),  # relative to the element
        ("x", "—", "—"),
    ]


def test_an_empty_field_has_no_bits():
    from reed.html import word_span

    assert word_span(32, 0, 16) == ("2", "—")


# --- unions, arrays and carried messages ------------------------------------

UNION = (
    '<integer name="id" bitCount="8"/>'
    '<union name="body" discrimField="../id">'
    '<command name="a" discrimValue="1"><integer name="x" bitCount="8"/></command>'
    '<command name="b" discrimValue="2"><integer name="y" bitCount="24"/></command>'
    "</union>"
)


def test_an_unsplit_union_lists_every_case_from_the_same_offset():
    rows = rows_of(UNION)

    assert brief(rows, "depth", "name", "kind", "offset") == [
        (0, "id", "integer", 0),
        (0, "body", "union", 8),
        (1, "a", "case", 8),
        (2, "x", "integer", 8),
        (1, "b", "case", 8),
        (2, "y", "integer", 8),
    ]
    assert rows[1]["type"] == "union on ../id"
    assert rows[2]["case_value"] == 1


def test_a_variant_shows_its_fixed_discriminator():
    rows = rows_of(UNION, "m/id", variant="m/b")

    assert brief(rows, "depth", "name", "kind", "const") == [
        (0, "id", "integer", 2),
        (0, "body", "group", None),
        (1, "y", "integer", None),
    ]


def test_a_system_id_const_is_split_over_its_fields():
    trees = expand(EXAMPLE.load(["tests/data/example-icd.xml"]), EXAMPLE)
    icds = EXAMPLE.load(["tests/data/example-icd.xml"])
    (ret,) = [v for v in trees["http://example.com/example-icd"] if v.name == "Return"]
    fc2 = next(v for v in walk(ret) if v.name == "Return/status/EPS.fuelCell.fc2")

    rows = {r["path"]: r for r in field_rows(fc2.message, icds)}

    assert rows["ReturnBody/systemId"]["const"] == [4, 1, 2]
    assert [rows[f"ReturnBody/systemId/{n}"]["const"] for n in ("system", "subsystem", "unit")] == [
        4,
        1,
        2,
    ]


def test_array_elements_have_offsets_relative_to_the_element():
    rows = rows_of(
        '<integer name="n" bitCount="8"/><spare bitCount="8"/>'
        '<group name="items" countValue="../n">'
        '<integer name="a" bitCount="8"/><integer name="b" bitCount="8"/></group>'
    )

    assert brief(rows, "depth", "name", "offset", "relative", "type") == [
        (0, "n", 0, False, "uint8"),
        (0, "(spare)", 8, False, "spare"),
        (0, "items", 16, False, "array × ../n"),
        (1, "a", 0, True, "uint8"),
        (1, "b", 8, True, "uint8"),
    ]


def test_a_carried_message_links_to_its_own_tree():
    rows = rows_of(
        '<group name="payload" ref="p"/>',
        extra='<message name="p"><integer name="x" bitCount="16"/></message>',
    )

    assert brief(rows, "name", "kind", "link", "bits") == [("payload", "message", "p", 16)]


# --- the page ---------------------------------------------------------------


def embedded(html: str) -> dict:
    (data,) = re.findall(r'<script id="icd-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(data)


def test_page_embeds_every_variant():
    icds = EXAMPLE.load(["tests/data/example-icd.xml"])
    html = page(icds, EXAMPLE, title="Apollo CSM")

    data = embedded(html)
    assert "<title>Apollo CSM</title>" in html
    assert [icd["label"] for icd in data["icds"]] == ["example", "test"]
    command = data["icds"][0]["messages"][0]
    assert command["name"] == "Command"
    assert command["key"] == "example:Command"
    assert [c["name"] for c in command["children"]][:2] == ["Command/power", "Command/gimbalTrim"]
    assert command["size"] == "80–368 bits (72–368 bits used)"


def test_page_links_carried_messages_by_key():
    icds = EXAMPLE.load(["tests/data/example-icd.xml"])
    command = embedded(page(icds, EXAMPLE))["icds"][0]["messages"][0]

    test = next(c for c in command["children"] if c["name"] == "Command/TestCommand")
    (carried,) = [r for r in test["rows"] if r["kind"] == "message"]
    assert carried["link"] == "test:TestCommand"


def test_embedded_data_cannot_close_the_script_tag():
    icds = Dialect("test").lower(
        ['<icd><message name="m"><integer name="x" bitCount="16">'
         "<description>a &lt;/script&gt; b</description></integer></message></icd>"]
    )

    html = page(icds, Dialect("test"))

    assert "a </script> b" not in html
    assert embedded(html)["icds"][0]["messages"][0]["rows"][0]["description"] == "a </script> b"


# --- reed html --------------------------------------------------------------


def test_cli_writes_the_page(tmp_path, capsys):
    out = tmp_path / "icd.html"

    code = main(["html", "--dialect", "example", "-o", str(out), "tests/data/example-icd.xml"])

    assert code == 0
    assert "<title>example-icd</title>" in out.read_text()
    assert f"wrote {out}" in capsys.readouterr().out


def test_cli_reports_errors(tmp_path, capsys):
    code = main(["html", "-o", str(tmp_path / "x.html"), "tests/data/example-icd.xml"])

    assert code == 1
    assert "unknown element <commandkey>" in capsys.readouterr().err
