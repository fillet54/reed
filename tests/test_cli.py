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


def test_lowering_warnings_are_printed(tmp_path, capsys):
    path = write(
        tmp_path,
        "orphan.xml",
        '<icd>\n<fieldDefs>\n<group name="orphan"/>\n</fieldDefs>\n<message name="m"/>\n</icd>',
    )

    code, out, _ = check(capsys, path)

    assert code == 0
    assert f"warning: {path}:3: <group name='orphan'> in fieldDefs has no id" in out
    assert "1 message, 0 errors, 1 warning" in out


def test_example_dialect_on_example_icd(capsys):
    code, out, _ = check(capsys, "--dialect", "example", EXAMPLE_ICD)

    assert code == 0
    assert "3 messages, 0 errors, 0 warnings" in out  # test-icd.xml comes in by import


def test_lowering_error_names_the_file(capsys):
    # The base dialect doesn't know <commandkey>.
    code, _, err = check(capsys, EXAMPLE_ICD)

    assert code == 1
    assert "example-icd.xml:16: unknown element <commandkey>" in err


def test_lowering_error_gives_file_and_line(tmp_path, capsys):
    path = write(
        tmp_path,
        "real.xml",
        '<icd>\n  <message name="m">\n    <scalar name="freq"/>\n  </message>\n</icd>',
    )

    code, _, err = check(capsys, path)

    assert code == 1
    assert f"{path}:3: missing required attribute 'bitCount' on <scalar name='freq'>" in err


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

    assert "Command  80–368 bits (72–368 bits used)" in out
    assert "  power  96 bits" in out
    assert "  TestCommand  96–112 bits" in out
    assert "Return  80–2112 bits (72–2112 bits used)" in out
    assert "TestCommand  32–48 bits" in out


def test_stats_reports_time_and_memory(capsys):
    _, out, _ = check(capsys, "--dialect", "example", "--stats", EXAMPLE_ICD)

    assert "lowered in" in out
    assert "peak memory" in out


# --- reed expand ------------------------------------------------------------


def expand(capsys, *args):
    code = main(["expand", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_expand_prints_the_variant_tree(capsys):
    code, out, _ = expand(capsys, "--dialect", "example", EXAMPLE_ICD)

    assert code == 0
    lines = out.splitlines()
    assert lines[:3] == [
        "Command  (Command)  80–368 bits (72–368 bits used)",
        "├─ Command/power  (Command, 1)  96 bits",
        "├─ Command/gimbalTrim  (Command, 2)  128 bits",
    ]
    assert "└─ Command/default  (Command, default)  80 bits" in lines
    assert "│  ├─ Return/status/GNC  (Return, 1, (1, 0, 0))  112 bits" in lines
    assert "│  └─ Return/status/EPS.fuelCell.fc3  (Return, 1, (4, 1, 3))  128 bits" in lines
    assert lines[-1] == "└─ TestCommand/memoryCheck  (TestCommand, 3)  48 bits"


def test_expand_refuses_an_icd_with_errors(tmp_path, capsys):
    path = write(
        tmp_path,
        "dup.xml",
        '<icd><message name="command"><group name="header">'
        '<integer name="id" bitCount="16"/><integer name="key" bitCount="16"/></group>'
        '<union name="commandBody" discrimField="../header/id">'
        '<command name="a" discrimValue="1"/><command name="b" discrimValue="1"/>'
        "</union></message></icd>",
    )

    code, out, err = expand(capsys, "--dialect", "example", path)

    assert code == 1
    assert out == ""
    assert "not expanding" in err
    assert "[duplicate-case-value]" in err
