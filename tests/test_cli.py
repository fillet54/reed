"""`reed check`: lower ICD files, validate them, print issues."""

import pytest

from reed.__main__ import main

EXAMPLE_ICD = "tests/data/example-icd.xml"


def write(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def check(capsys, *args):
    code = main(["check", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_valid_icd_exits_zero(tmp_path, capsys):
    path = write(tmp_path, "ok.xml", '<icd><message name="m"><spare bitCount="8"/></message></icd>')

    code, out, _ = check(capsys, path)

    assert code == 0
    assert "1 message, 0 errors, 0 warnings" in out


def test_errors_are_printed_and_exit_one(tmp_path, capsys):
    path = write(
        tmp_path,
        "bad.xml",
        '<icd><message name="m"><group name="g" bitCount="4"><spare bitCount="8"/></group>'
        "</message></icd>",
    )

    code, out, _ = check(capsys, path)

    assert code == 1
    assert "error: m/g: bitCount is 4 but the fields add up to 8 bits [bit-count]" in out
    assert "1 message, 1 error, 0 warnings" in out


def test_warnings_alone_exit_zero(tmp_path, capsys):
    path = write(
        tmp_path,
        "warn.xml",
        '<icd><message name="m"><group name="g" bitCount="variable"><spare bitCount="8"/>'
        "</group></message></icd>",
    )

    code, out, _ = check(capsys, path)

    assert code == 0
    assert "warning: m/g:" in out


def test_example_dialect_on_example_icd(capsys):
    code, out, _ = check(capsys, "--dialect", "example", EXAMPLE_ICD)

    assert code == 1
    assert "[duplicate-domain-value]" in out
    assert "2 messages, 2 errors, 0 warnings" in out


def test_lowering_error_names_the_file(capsys):
    # The base dialect doesn't know xsi:type="systemIdRef".
    code, _, err = check(capsys, EXAMPLE_ICD)

    assert code == 1
    assert "example-icd.xml: unknown xsi:type 'systemIdRef'" in err


def test_invalid_xml_names_the_file(tmp_path, capsys):
    path = write(tmp_path, "broken.xml", "<icd><message name='m'></icd>")

    code, _, err = check(capsys, path)

    assert code == 1
    assert "broken.xml:" in err


def test_missing_file_is_an_error(capsys):
    code, _, err = check(capsys, "no/such/file.xml")

    assert code == 1
    assert "no/such/file.xml" in err


def test_unknown_dialect_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["check", "--dialect", "nope", EXAMPLE_ICD])

    assert exc.value.code == 2


def test_sizes_lists_messages_and_their_cases(capsys):
    code, out, _ = check(capsys, "--dialect", "example", "--sizes", EXAMPLE_ICD)

    assert "command  48–64 bits" in out
    assert "  power  64 bits" in out
    assert "  ibit  64 bits (53 used)" in out
    assert "return  64–96 bits (53–85 bits used)" in out


def test_stats_reports_time_and_memory(capsys):
    _, out, _ = check(capsys, "--dialect", "example", "--stats", EXAMPLE_ICD)

    assert "lowered in" in out
    assert "peak memory" in out
