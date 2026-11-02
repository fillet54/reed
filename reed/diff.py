"""Diffs between two versions of an ICD, at the expanded level.

Both versions are expanded and their variants matched by name (or, for a
renamed case, by id). Each pair of fully expanded variants is compared as
field rows (see `reed.html.field_rows`), matched by path. Every change gets a
category:

- editorial: only labels and descriptions changed;
- compatible: the old layout still reads the same: new fields in spare bits or
  after the last field, new variants, cases or named values, renamed fields,
  changed defaults;
- incompatible: anything that moves old bits: a field moved, resized, removed
  or retyped, a field inserted ahead of others, a fixed discriminator value
  changed, a variant removed, a message made shorter.

A variant's status is its worst change, or "unchanged".

`diff(old_icds, new_icds, dialect)` returns the merged variant tree as page
data: each leaf has its status, its changes and merged rows. A merged row is
the new row (or, if it was removed, the old one) with `change` ("added",
"removed", "changed" or None) and `was`: the old values of what differs.
"""

from __future__ import annotations

from collections.abc import Mapping

from .expand import Variant, expand
from .html import _jsonable, _show, field_rows, message_key
from .ir import Icd
from .size import describe_size, layout_of

UNCHANGED, EDITORIAL, COMPATIBLE, INCOMPATIBLE = "unchanged", "editorial", "compatible", "incompatible"
CATEGORIES = (UNCHANGED, EDITORIAL, COMPATIBLE, INCOMPATIBLE)  # best to worst
RANK = {c: i for i, c in enumerate(CATEGORIES)}

CONTAINERS = {"group", "union", "case", "array"}
SPARES = {"spare", "pad"}
COMPARED = (
    "name", "path", "kind", "type", "offset", "bits", "size", "relative", "word", "bit_span",
    "const", "values", "restrict", "default", "label", "description", "case_value",
)  # fmt: skip


def worst(categories) -> str:
    return max(categories, key=RANK.__getitem__, default=UNCHANGED)


# --- variant trees ---------------------------------------------------------------


def diff(old_icds: Mapping[str, Icd], new_icds: Mapping[str, Icd], dialect) -> list[dict]:
    """The merged variant trees of both versions, as page ICDs (see the module docstring)."""
    old_trees, new_trees = expand(old_icds, dialect), expand(new_icds, dialect)
    out = []
    for namespace in list(new_trees) + [n for n in old_trees if n not in new_trees]:
        icds = new_icds if namespace in new_icds else old_icds
        icd = icds[namespace]
        sides = _Sides(old_icds, new_icds, namespace)
        out.append(
            {
                "label": icd.alias or icd.namespace,
                "namespace": namespace,
                "messages": sides.merge(old_trees.get(namespace, ()), new_trees.get(namespace, ())),
            }
        )
    return out


def leaves(tree: list[dict]) -> list[dict]:
    """The fully expanded variants of a merged tree, in order."""

    def walk(v):
        return [x for c in v["children"] for x in walk(c)] if v["children"] else [v]

    return [x for icd in tree for m in icd["messages"] for x in walk(m)]


def counts(tree: list[dict]) -> dict[str, int]:
    found = {c: 0 for c in CATEGORIES}
    for v in leaves(tree):
        found[v["status"]] += 1
    return found


def summary_lines(tree: list[dict]) -> list[str]:
    """The diff as text: a count per category, then each changed variant and its changes."""
    found = counts(tree)
    lines = [", ".join(f"{n} {c}" for c, n in reversed(found.items()) if n) or "no variants"]
    for v in leaves(tree):
        if v["status"] != UNCHANGED:
            lines.append(f"{v['status']:<13} {v['key']}")
            lines += [f"    {c['text']}" for c in v["changes"]]
    return lines


class _Sides:
    def __init__(self, old_icds, new_icds, namespace):
        self.old_icds, self.new_icds, self.namespace = old_icds, new_icds, namespace

    def merge(self, old: tuple[Variant, ...], new: tuple[Variant, ...]) -> list[dict]:
        old_by = {v.name: v for v in old}
        new_names = {v.name for v in new}
        gone = [v for v in old if v.name not in new_names]
        # A case renamed but not renumbered: the same variant under a new name.
        by_id = {v.id: v for v in gone}
        out = []
        for v in new:
            was = old_by.get(v.name) or by_id.pop(v.id, None)
            out.append(self.node(was, v))
        return out + [self.node(v, None) for v in gone if v.id in by_id]

    def node(self, old: Variant | None, new: Variant | None) -> dict:
        side = new or old
        icds = self.new_icds if new else self.old_icds
        message = side.message
        node = {
            "key": message_key(icds, self.namespace, side.name),
            "name": side.name,
            "id": ", ".join(_show(v) for v in side.id),
            "size": describe_size(message, icds),
            "old_size": describe_size(old.message, self.old_icds) if old else None,
            "label": message.body.meta.label,
            "description": message.body.meta.description,
            "changes": [],
        }
        if old and new and old.name != new.name:
            node["changes"].append(_change("", "renamed", COMPATIBLE, f"variant renamed from {old.name}"))

        if (old is None or old.children) and (new is None or new.children):
            node["children"] = self.merge(old.children if old else (), new.children if new else ())
            node["status"] = worst(
                [c["category"] for c in node["changes"]] + [c["status"] for c in node["children"]]
            )
            return node

        if old and new:
            was = old.message.body.meta
            which = [f for f in ("label", "description") if getattr(was, f) != node[f]]
            if which:
                node["changes"].append(
                    _change("", "docs", EDITORIAL, f"variant {' and '.join(which)} changed")
                )
                node["was"] = {f: getattr(was, f) for f in which}
        node["children"] = []
        node["consts"] = [[p, _jsonable(v)] for p, v in message.consts]
        node["word_bits"] = layout_of(icds, message).word_bits
        if old and new and not old.children and not new.children:
            rows, changes = diff_rows(
                field_rows(old.message, self.old_icds), field_rows(new.message, self.new_icds)
            )
            node["rows"] = rows
            node["changes"] += changes
        elif old and new:  # one side splits into variants, the other doesn't
            node["rows"] = [_plain(r) for r in field_rows(new.message, self.new_icds)]
            node["changes"].append(
                _change(
                    "",
                    "expansion",
                    INCOMPATIBLE,
                    "now one layout, was split into variants"
                    if old.children
                    else "now split into variants; was one layout",
                )
            )
        else:
            node["rows"] = [_plain(r) for r in field_rows(message, icds)]
            node["changes"].append(_whole(old, new))
        node["status"] = worst(c["category"] for c in node["changes"])
        return node


def _whole(old, new) -> dict:
    if new is None:
        return _change("", "variant removed", INCOMPATIBLE, "variant removed")
    return _change("", "variant added", COMPATIBLE, "new variant")


def _plain(row: dict) -> dict:
    return {**row, "change": None, "was": {}}


def _change(path, what, category, text, row=None) -> dict:
    return {"path": path, "what": what, "category": category, "text": text, "row": row}


# --- rows ------------------------------------------------------------------------


def diff_rows(old: list[dict], new: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merged rows and the changes between two variants' rows."""
    old_scope, new_scope = _scopes(old), _scopes(new)
    match: dict[int, int] = {}  # old index -> new index
    renamed: set[int] = set()  # new indexes

    # By path; a spare by where it is.
    new_at = {}
    for j, key in enumerate(_keys(new)):
        if key is not None:
            new_at[key] = j
    for i, key in enumerate(_keys(old)):
        if key is not None and key in new_at:
            match[i] = new_at[key]
    taken = set(match.values())

    def place(rows, scopes, i):
        r = rows[i]
        return (scopes[i], r["relative"], r["offset"], r["bits"])

    def pair(test) -> list[int]:
        """Match unmatched rows that pass `test` by place and type; the new indexes matched."""
        free, paired = {}, []
        for j, r in enumerate(new):
            if j not in taken and test(r):
                free.setdefault(place(new, new_scope, j) + (r["type"],), []).append(j)
        for i, r in enumerate(old):
            if i in match or not test(r) or r["offset"] is None or r["bits"] is None:
                continue
            spot = free.get(place(old, old_scope, i) + (r["type"],))
            if spot:
                match[i] = j = spot.pop(0)
                taken.add(j)
                paired.append(j)
        return paired

    pair(lambda r: r["kind"] in SPARES)
    renamed.update(pair(lambda r: r["kind"] not in SPARES and _leafish(r)))

    merged = _merge_order(old, new, match)
    old_of = {j: i for i, j in match.items()}
    spare_bits = _spare_bits(old, old_scope)
    old_end, new_end = _end(old), _end(new)

    # Which matched, laid-out rows come after each new row: for "appended".
    later_scopes: list[set] = [set() for _ in new]
    seen: set = set()
    for j in range(len(new) - 1, -1, -1):
        later_scopes[j] = set(seen)
        r = new[j]
        if j in taken and r["kind"] not in SPARES and _leafish(r):
            seen.add(new_scope[j])

    rows, changes = [], []
    whole = set()  # paths of cases added or removed: their contents go with them
    for side, k in merged:
        index = len(rows)
        if side == "new":
            r, j = new[k], k
            i = old_of.get(j)
            if i is None:
                row = {**r, "change": "added", "was": {}}
                found = _added(r, new_scope[j], later_scopes[j], spare_bits, whole)
            else:
                was = {f: old[i][f] for f in COMPARED if old[i][f] != r[f]}
                row = {**r, "change": None, "was": was}
                found = _changed(old[i], r, was, j in renamed)
        else:
            r = old[k]
            row = {**r, "change": "removed", "was": {}}
            found = _removed(r, whole)
        for c in found:
            c["row"] = index
        if found and row["change"] is None:
            row["change"] = "changed"
        rows.append(row)
        changes += found

    # Texts name fields as the table shows them: without unnested containers.
    hidden = {tuple(r["path"].split("/")) for r in old + new if r["hidden"] and r["path"]}
    for c in changes:
        if c["path"] and c["text"].startswith(c["path"] + ":"):
            c["text"] = _shown(c["path"], hidden) + c["text"][len(c["path"]) :]

    if old_end is not None and new_end is not None and new_end < old_end:
        changes.append(
            _change("", "size", INCOMPATIBLE, f"message shorter: {new_end} bits, was {old_end}")
        )
    return rows, changes


def _keys(rows):
    """Each row's identity: its path, unless it's a spare (matched by place)."""
    seen: dict = {}
    keys = []
    for r in rows:
        if r["kind"] in SPARES:
            keys.append(None)
            continue
        key = (r["path"], r["kind"] in CONTAINERS and not r["link"])
        seen[key] = seen.get(key, 0) + 1
        keys.append(key + (seen[key],))
    return keys


def _scopes(rows):
    """Each row's innermost case or array: offsets in different ones don't compete."""
    stack: list[tuple] = []
    out = []
    for r in rows:
        path = tuple(r["path"].split("/")) if r["path"] else ()
        while stack and stack[-1] != path[: len(stack[-1])]:
            stack.pop()
        if stack and stack[-1] == path and r["kind"] in ("case", "array"):
            stack.pop()  # a container isn't in its own scope
        out.append("/".join(stack[-1]) if stack else "")
        if r["kind"] in ("case", "array"):
            stack.append(path)
    return out


def _leafish(r) -> bool:
    """A row that has bits of its own: not a group, case, array or (unlinked) union."""
    return r["kind"] not in CONTAINERS or bool(r["link"])


def _spare_bits(rows, scopes) -> dict:
    """The old spare bits, as {(scope, relative): [(start, end), ...]}."""
    bits: dict = {}
    for r, scope in zip(rows, scopes):
        if r["kind"] in SPARES and r["offset"] is not None and r["bits"]:
            bits.setdefault((scope, r["relative"]), []).append((r["offset"], r["offset"] + r["bits"]))
    return bits


def _covered(start, end, spans) -> bool:
    for lo, hi in sorted(spans):
        if lo > start:
            return False
        start = max(start, hi)
        if start >= end:
            return True
    return start >= end


def _end(rows):
    """The bits a variant takes, if fixed."""
    tops = [r for r in rows if r["map"]]
    if any(r["offset"] is None or r["bits"] is None for r in tops):
        return None
    return max((r["offset"] + r["bits"] for r in tops), default=0)


def _merge_order(old, new, match):
    """New rows in order, with each removed old row after the new row that
    matches the old row before it."""
    after: dict[int, list[int]] = {}
    anchor = -1
    for i in range(len(old)):
        if i in match:
            anchor = match[i]
        else:
            after.setdefault(anchor, []).append(i)
    out = [("old", i) for i in after.get(-1, [])]
    for j in range(len(new)):
        out.append(("new", j))
        out += [("old", i) for i in after.get(j, [])]
    return out


def _shown(path: str, hidden: set) -> str:
    parts = path.split("/")
    keep = [p for k, p in enumerate(parts) if tuple(parts[: k + 1]) not in hidden]
    return "/".join(keep) or path


def _inside(path: str, paths) -> bool:
    return any(path.startswith(p + "/") for p in paths)


def _where(r) -> str:
    return f"word {r['word']} bits {r['bit_span']}"


def _added(r, scope, later, spare_bits, whole) -> list[dict]:
    path = r["path"]
    if _inside(path, whole) or r["hidden"]:
        return []
    if r["kind"] == "case":
        whole.add(path)
        return [_change(path, "added", COMPATIBLE, f"{path}: new case")]
    if r["kind"] in SPARES or not _leafish(r):
        return []
    if r["offset"] is not None and r["bits"] is not None:
        spans = spare_bits.get((scope, r["relative"]), [])
        if _covered(r["offset"], r["offset"] + r["bits"], spans):
            return [_change(path, "added", COMPATIBLE, f"{path}: new, in spare bits ({_where(r)})")]
    ahead = [s for s in later if scope.startswith(s) and (s == scope or scope[len(s)] == "/" or not s)]
    if not r["relative"] and not ahead:
        return [_change(path, "added", COMPATIBLE, f"{path}: new, at the end ({_where(r)})")]
    return [_change(path, "added", INCOMPATIBLE, f"{path}: new, ahead of existing fields ({_where(r)})")]


def _removed(r, whole) -> list[dict]:
    path = r["path"]
    if _inside(path, whole) or r["hidden"]:
        return []
    if r["kind"] == "case":
        whole.add(path)
        return [_change(path, "removed", INCOMPATIBLE, f"{path}: case removed")]
    if r["kind"] in SPARES or not _leafish(r):
        return []
    return [_change(path, "removed", INCOMPATIBLE, f"{path}: removed (was {_where(r)})")]


def _same_fixed(old, new) -> bool:
    """Fixed at the same named value: other values don't matter here."""
    if new["const"] is None or new["const"] != old["const"]:
        return False
    name = {v: n for n, v in old["values"]}.get(old["const"])
    return name == {v: n for n, v in new["values"]}.get(new["const"])


def _changed(old, new, was, renamed) -> list[dict]:
    if new["hidden"]:
        return []
    path = new["path"]
    out = []
    if renamed:
        out.append(_change(path, "renamed", COMPATIBLE, f"{path}: renamed from {old['name']}"))
    if _leafish(new) and new["kind"] not in SPARES:
        if "offset" in was or "relative" in was:
            out.append(
                _change(path, "moved", INCOMPATIBLE, f"{path}: moved to {_where(new)}, was {_where(old)}")
            )
        elif "bits" in was:
            out.append(
                _change(
                    path,
                    "resized",
                    INCOMPATIBLE,
                    f"{path}: {new['size']}, was {old['size']} ({_where(new)})",
                )
            )
    if "type" in was and ("kind" in was or "bits" not in was):  # a resize changes uint8 to uint4
        out.append(_change(path, "type", INCOMPATIBLE, f"{path}: {new['type']}, was {old['type']}"))
    if "const" in was:
        out.append(
            _change(path, "fixed", INCOMPATIBLE, f"{path}: fixed at {new['const']}, was {old['const']}")
        )
    if "values" in was and not _same_fixed(old, new):
        before = {(n, v) for n, v in old["values"]}
        after = {(n, v) for n, v in new["values"]}
        if before <= after:
            added = ", ".join(f"{n}={v}" for n, v in new["values"] if (n, v) not in before)
            out.append(_change(path, "values", COMPATIBLE, f"{path}: new named values {added}"))
        else:
            gone = ", ".join(f"{n}={v}" for n, v in old["values"] if (n, v) not in after)
            out.append(_change(path, "values", INCOMPATIBLE, f"{path}: named values changed ({gone} gone)"))
    if "restrict" in was:
        out.append(
            _change(path, "restrict", COMPATIBLE, f"{path}: restrict {new['restrict']}, was {old['restrict']}")
        )
    if "default" in was:
        out.append(
            _change(path, "default", COMPATIBLE, f"{path}: default {new['default']}, was {old['default']}")
        )
    if "label" in was or "description" in was:
        which = " and ".join(f for f in ("label", "description") if f in was)
        out.append(_change(path, "docs", EDITORIAL, f"{path}: {which} changed"))
    return out
