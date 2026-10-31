from dataclasses import replace

from reed.ir import Int, Meta, Struct


def test_shared_def_renamed_at_use_site_keeps_children():
    header = Struct("header", (Int("id", 16), Int("key", 16)), meta=Meta(origin="header"))

    use = replace(header, name="hdr")

    assert use.name == "hdr"
    assert use.fields is header.fields
    assert use.meta.origin == "header"
