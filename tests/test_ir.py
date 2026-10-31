from dataclasses import replace

from reed.ir import Int, Message, Meta, Spare, Struct


def test_message_name_is_first_key_part():
    message = Message(key=("command", "power"), body=Struct("command", (Spare(4),)))

    assert message.name == "command"


def test_shared_def_renamed_at_use_site_keeps_children():
    header = Struct("header", (Int("id", 16), Int("key", 16)), meta=Meta(origin="header"))

    use = replace(header, name="hdr")

    assert use.name == "hdr"
    assert use.fields is header.fields
    assert use.meta.origin == "header"
