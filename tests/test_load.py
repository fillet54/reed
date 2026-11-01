"""Loading ICD files from disk, following their refSchemaLocation imports."""

import pytest

from reed.dialect import BASE
from reed.lower import LoweringError, load_files

MAIN = (
    '<icd><namespace alias="main">urn:main</namespace>'
    '<refSchemaLocation alias="support">https://example.com/support-icd support-msg.xsd'
    "</refSchemaLocation>"
    '<message name="m"><group name="x" ref="support:XYZ"/></message></icd>'
)
SUPPORT = (
    '<icd><namespace alias="support">https://example.com/support-icd</namespace>'
    "{imports}"
    '<fieldDefs><group id="XYZ"><spare bitCount="4"/></group></fieldDefs></icd>'
)


def test_imported_file_is_loaded_from_the_same_directory(tmp_path):
    (tmp_path / "main.xml").write_text(MAIN)
    (tmp_path / "support-icd.xml").write_text(SUPPORT.format(imports=""))

    icds = load_files([tmp_path / "main.xml"])

    assert sorted(icds) == ["https://example.com/support-icd", "urn:main"]


def test_imports_are_followed_transitively(tmp_path):
    (tmp_path / "main.xml").write_text(MAIN)
    (tmp_path / "support-icd.xml").write_text(
        SUPPORT.format(
            imports='<refSchemaLocation alias="base">https://example.com/base-icd b.xsd'
            "</refSchemaLocation>"
        )
    )
    (tmp_path / "base-icd.xml").write_text(
        '<icd><namespace alias="base">https://example.com/base-icd</namespace></icd>'
    )

    assert len(load_files([tmp_path / "main.xml"])) == 3


def test_a_file_given_and_imported_is_loaded_once(tmp_path):
    (tmp_path / "main.xml").write_text(MAIN)
    (tmp_path / "support-icd.xml").write_text(SUPPORT.format(imports=""))

    icds = load_files([tmp_path / "main.xml", tmp_path / "support-icd.xml"])

    assert len(icds) == 2


def test_missing_import_file_fails_when_used(tmp_path):
    (tmp_path / "main.xml").write_text(MAIN)

    with pytest.raises(LoweringError, match="main.xml:1: alias 'support' imports"):
        load_files([tmp_path / "main.xml"])


def test_dialect_load_uses_its_lowering_and_layout(tmp_path):
    (tmp_path / "main.xml").write_text(MAIN)
    (tmp_path / "support-icd.xml").write_text(SUPPORT.format(imports=""))

    icds = BASE.load([tmp_path / "main.xml"])

    assert icds["urn:main"].layout == BASE.layout
