"""Walking the IR with context, and resolving relative paths like "../header/id".

IR nodes have no parent pointers, so a walk carries a stack of Frames from the
message body down to the current node. Union cases are a level of their own:
from a field inside a case, ".." is the case and "../.." the union.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ir import Array, Node, Path, Struct, Union

Stack = tuple["Frame", ...]


@dataclass(frozen=True, slots=True)
class Frame:
    node: object
    index: tuple[int, ...]  # position from the message body, in stream order
    names: tuple[str, ...]  # display path, starting with the message name


def child_name(node) -> str:
    name = getattr(node, "name", "")
    if name:
        return name
    return f"({type(node).__name__.lower()})"


def children(frame: Frame) -> list[Frame]:
    """Frames for the nodes directly inside `frame`. Case bodies for a union."""
    node = frame.node
    if isinstance(node, Struct):
        items = [(child_name(f), f) for f in node.fields]
    elif isinstance(node, Union):
        items = [(case.name, case.body) for case in node.cases]
    elif isinstance(node, Array):
        # The element is a level for relative paths, but not in display paths:
        # inside array "items" a field is "items/x", not "items/items/x".
        return [Frame(node.element, frame.index + (0,), frame.names)]
    else:
        items = []

    return [
        Frame(child, frame.index + (i,), frame.names + (name,))
        for i, (name, child) in enumerate(items)
    ]


def resolve(path: Path, stack: Stack) -> Frame | None:
    """Follow `path` starting at the node on top of `stack`."""
    frames = list(stack)

    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            frames.pop()
            if not frames:
                return None
            continue

        for child in children(frames[-1]):
            if getattr(child.node, "name", None) == segment:
                frames.append(child)
                break
        else:
            return None

    return frames[-1]


def is_before(target: Frame, user: Frame) -> bool:
    """True if `target` is complete in the stream before `user` starts."""
    n = len(target.index)
    return target.index < user.index and user.index[:n] != target.index


def field_path(frame: Frame) -> str:
    """The path of a frame relative to its message, e.g. "header/id"."""
    return "/".join(frame.names[1:])


def root_stack(body: Struct, message_name: str) -> Stack:
    return (Frame(body, (), (message_name,)),)


def push(stack: Stack, frame: Frame) -> Stack:
    return stack + (frame,)


__all__ = ["Frame", "Node", "Stack", "children", "field_path", "is_before", "resolve"]
