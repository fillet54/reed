# reed architecture

reed reads XML ICD (interface control document) definitions and turns them into one normalized message model (the IR, in `reed/ir.py`). Every tool works on that model: codecs, table views, message expansion and docs. Dialect quirks stay in the lowering step.

## Pipeline

```
XML ──parse──▶ DOM ──lower──▶ IR ──link/validate──▶ IR ──expand──▶ concrete messages
                (per dialect)       (sizes, refs)       (split rules)    │
                                                                         ├─▶ codec (dict ⇄ bytes)
                                                                         ├─▶ table model ─▶ docutils / HTML / text
                                                                         └─▶ tree view, lookup, …
```

1. **Parse.** ElementTree (or lxml for source line numbers). A real ICD is ~5 MB of XML, so the DOM (~45 MB) is held only until lowering finishes.
2. **Lower** (one visitor per dialect). DOM → IR. Dialect concepts map onto IR nodes, e.g. `systemIdRef` becomes a `Struct` with a `Domain` tree.
3. **Link / validate.** Every ref resolves across files, there are no cycles, declared `bitCount`s match computed sizes, and discriminator and count paths resolve.
4. **Expand.** Split rules produce concrete messages. Expansion is lazy: a generator, or lookup by key.
5. **Tools** read the IR through a visitor base that passes the path from the root down.

## Decisions

### FieldDef refs are inlined, but shared
- Before lowering, index fieldDef DOM elements by id across all files. Lower each one the first time it's referenced, cache the result, and mark it in progress while lowering so cycles are caught.
- Put the **same** frozen object at every use site. A use-site rename is `dataclasses.replace(d, name=...)`: a new top node with shared children.
- `meta.origin` records the fieldDef id so tables can show or link to where a field came from. Tools never need to follow it.
- Don't cache per-position results by `id(node)`, because one node appears in many places. Key them by path.

### Message refs stay as `MessageRef`
- In the XML they look like any other ref: `<group ref="command">`, or with another file's namespace, `<command name="truck" ref="payload:command">`. Lowering tells them apart by what the key resolves to: a fieldDef id is inlined, a message name becomes a `MessageRef`, both is an ambiguity error, neither is unresolved.
- Used mainly for carrying ("trucking") an independent ICD inside a top-level handler. The boundary keeps:
  - **path scope**: the inner message's `../header/id` can't reach the outer header;
  - **layout**: bit and word numbering restarts, and the payload may use its own `LayoutPolicy`;
  - **independence**: the payload ICD is versioned, documented and tested on its own.
- Renderers link to the payload instead of repeating it. Codecs delegate to the inner module. Expansion treats it as one unit unless asked to go inside.

### Namespaces
- `<namespace alias="example">uri</namespace>` is the file **declaring its own** namespace. There is at most one per file. It sets `Icd.namespace` (the uri) and `Icd.alias` (the prefix). `lower_many` keys each ICD by the uri it declares; two files declaring the same uri is an error.
- The alias exists for convenience and documentation. Refs may use it (`ref="example:header"` resolves locally). An earlier binary ⇄ XML tool for this format used it as the element prefix in instance documents (`<example:command>`) instead of an `xmlns` declaration, so a future instance converter should do the same.
- **Imports (not done yet):** `schemaLocation` elements at the top of a file will import other files and give each an alias, so a fieldDef or message from another file can be used (`ref="payload:header"`). They add entries to the lowering's alias table; the shared `Catalog` already holds the other loaded ICDs and detects fieldDef cycles across them.

### Paths
`discrimField` and `countValue` resolve the same way: relative to the node that carries them. `..` is its parent, so a sibling field is `../n`. A union case is a level of its own: from a field inside a case, `..` is the case and `../..` the union. IR nodes have no parent pointers, so tools walk with a stack of the current node's parents (`reed/paths.py`).

### Sizes
- `bitCount="<n>"` becomes `declared_bits`, which validation checks. `bitCount="variable"` becomes `VARIABLE` and is only a hint; the real size is computed.
- Size analysis produces `Size(lo, hi, linear)`:
  - Struct = sum of its fields; Union = min/max over its cases (no exact formula).
  - `Array` (from `countValue`) = the count's range × element size.
  - `linear` is an exact formula `a + Σ bᵢ·countᵢ` when one exists. After unions are expanded, only arrays remain variable, so expanded messages usually get an exact formula.
- **Word padding:** with `LayoutPolicy.pad` (the default) a message fills whole words (`word_bits`, 16 in the example dialect); the rest of its last word is an implicit spare. With `pad=False` there is no implicit spare: every message must be whole words itself (`not-whole-words` error) and a carried message packs tightly. Message sizes include it (`size_of(..., padded=False)` gives the bits the fields use), and so do carried messages inside another one. Field and group sizes don't. Rounding up `a + b·n` is not linear, so a padded size keeps its formula only when every part is whole words. Limits such as `max_bits` and the 50-byte unprotocolled rule measure the padded size.
- `LayoutPolicy.max_bits` (512 bytes) caps `hi` and bounds array counts: effective max count = (max − rest) ÷ smallest element size.
- Validation:
  - declared size ≠ computed size → error;
  - `variable` that is actually fixed → warning;
  - minimum size > max → error;
  - count field must come before its array.

### Expansion
One idea covers it: pick a field, list its possible values, and emit one variant per value with that field fixed (`Message.consts`) and any union keyed on it replaced by the matching case.
- `SplitOnUnion("header/id")`: command/return bodies.
- `SplitOnDomain("systemId")`: system/subsystem/unit paths. Whether value 0 means "whole system/subsystem" is a rule option.

Each dialect declares its own list of rules. Transforms rebuild only the path from the root to the change and share everything else.

### Codec
`reed/codec.py`: `encode(message, value, icds) -> bytes` and `decode(message, data, icds) -> dict`, using a sequential bit cursor (no precomputed offsets).
- **Value shapes:**
  - a group is a dict of its fields, with spares left out;
  - an integer is an int, or a name from its named values; decoding returns the name when there is one;
  - a scalar is a number, where engineering value = raw × scale + offset;
  - a union is `{case: value}`; an unnamed union's case name appears directly in the enclosing dict;
  - an array is a list;
  - a carried message is a nested dict.
- **Derived fields:** encoding fills discriminators from the chosen case (a tuple for a system ID) and counts from list lengths. A value given for them must agree. Decoding returns them.
- **Layout** (`LayoutPolicy`). The dialect owns it: `Dialect(..., layout=LayoutPolicy(word_bits=16, byteorder="big", pad=True))`. The XML has no syntax for it, and tools don't take a per-call override; lowering stamps it on every `Icd` and all tools read `icd.layout`, so sizes, validation and codecs always agree.
  - bits are packed MSB-first, so the first field is in the high bits of word 0 (no option until an ICD needs LSB-first);
  - words are `byteorder="big"` by default;
  - fields cross word boundaries freely;
  - with `pad`, the message is padded with zeros to a whole word (the implicit spare), and a carried payload, being a message too, is padded to its own whole words, so the fields after it start on a word.
- **Errors** carry the field path, e.g. `m/mode: 3 is not one of the named values`.

### Tables
A visitor builds a plain table model: word, bit range, name path and depth, value, description, row spans. Renderers turn it into docutils (Sphinx directive), HTML or text. Word numbers after an array use the exact formula ("Word 5 + 2·N").

## Memory (measured)
Synthetic 50 MB fully expanded ICD:
- ElementTree DOM: 455 MB (~500 B per element);
- slotted, frozen dataclass IR: 54 MB (~87 B per node).

So the IR is cheap. The parse is the peak, and at ~5 MB real input that is fine.

## Open questions
- Does a count field always come directly before its array?
