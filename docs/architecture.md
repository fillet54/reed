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
2. **Lower** (one visitor per dialect). DOM → IR. Dialect concepts map onto IR nodes, e.g. a `subsystemDef` becomes a `Struct` with a `Domain` tree.
3. **Link / validate.** Every ref resolves across files, there are no cycles, declared `bitCount`s match computed sizes, and discriminator and count paths resolve.
4. **Expand.** Split rules produce concrete messages. Expansion is lazy: a generator, or lookup by key.
5. **Tools** read the IR through a visitor base that passes the path from the root down.

## Decisions

### FieldDef refs are inlined, but shared
- Before lowering, index fieldDef DOM elements by id across all files. Lower each one the first time it's referenced, cache the result, and mark it in progress while lowering so cycles are caught.
- Put the **same** frozen object at every use site. A use-site rename is `dataclasses.replace(d, name=...)`: a new top node with shared children.
- **Use sites can override the def.** The use's tag and attributes win and the def's attributes fill in the rest, as in the old tool. A use whose tag differs from the def's gets a `ref-tag-mismatch` warning, so you can check the use should win. Tags the dialect lowers the same way (`group2`/`group`) count as the same. The exceptions are `name`, `ref`, `xsi:type` and `countValue`, which describe the use and aren't merged. The use's `<label>`/`<description>` replace the def's. Any other child is an error: `<group ref="x"><integer/></group>`. A use with attribute overrides lowers a merged copy of the def element. That copy is cached by (id, overrides, tag), so identical overrides share one node, and a use with the same tag and no overrides shares the plain def. `countValue` on a ref makes an array of the def. A union case's attributes belong to the case, so a case ref only gets the children check.
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
- `<namespace alias="example">uri</namespace>` is the file **declaring its own** namespace. The `alias` is optional and rarely used. There is at most one per file. It sets `Icd.namespace` (the uri) and `Icd.alias` (the prefix). `lower_many` keys each ICD by the uri it declares; two files declaring the same uri is an error.
- The alias exists for convenience and documentation. Refs may use it (`ref="example:header"` resolves locally). An earlier binary ⇄ XML tool for this format used it as the element prefix in instance documents (`<example:command>`) instead of an `xmlns` declaration, so a future instance converter should do the same.
- **Imports:** `<refSchemaLocation alias="support">https://example.com/support-icd support-msg.xsd</refSchemaLocation>` makes `support:` mean the loaded ICD whose `<namespace>` is that uri (the `.xsd` part is ignored). Without an import line, a prefix matches a loaded ICD's own alias. `load_files` / `Dialect.load` / `reed check` find an imported file next to the importer, named after the uri's last segment (`support-icd.xml`), follow imports transitively and load each file once. If the file isn't found, a ref through the alias fails with a hint naming the expected file. All ICDs share one `Catalog`, which also catches fieldDef cycles across files.

### Example ICDs
- `tests/data/example-icd.xml` is a small Apollo CSM ICD in the example dialect: `Command` (power, SPS gimbal trim and burn, a stored command via `<apCommand>`, raw uplink data, callsign, a default no-op) and `Return` (status by system ID, faults, battery voltages, crew messages). It uses every feature the dialect has.
- It imports `tests/data/test-icd.xml` (alias `test`), whose `TestCommand` message has its own header and union of test commands. The `TestCommand` command carries it: `<group name="TestCommandBody" ref="test:TestCommand"/>`. The test ICD refs the main ICD's `example:SubsystemType` back.
- `reed check --dialect example tests/data/example-icd.xml` loads both and should report no issues.

### System-ID unions (example dialect)
- `<union discrimField="../systemId" xsi:type="subsystemRef">` holds a `<system>`/`<subsystem>`/`<unit>` tree. It stands for three nested unions (on system, subsystem and unit), kept flat: one case per level that has fields of its own, named `fuel.tanks` and valued by the whole ID `(10, 1, 0)`. A level's fields are the 0 ("any/all") value of the levels below it. A unit is always a case. A level without fields is no case, so `(1, 1, 0)` for a subsystem that only has units doesn't decode.
- A `<system>` in a union without that xsi:type is an error, and so is any other child in a subsystemRef union.
- Levels are named only: their values come from the subsystemDef. The dialect assumes there is exactly one: on first use it gathers every loaded ICD's subsystemDefs by `alias:id` into `Catalog.context` (shared lowering state for dialects), and lowering fails if it finds none or several. A name the subsystemDef doesn't have is a lowering error, also on a level that is no case. A `value` may still be given; it must agree with the subsystemDef's.
- The `subsystem-ref-discrim` rule checks that the union's `discrimField` is a subsystemIDRef of that same subsystemDef.

### Floats
- `<ieeeFloat bitCount="32|64">` is an IEEE 754 single or double (`Float`). Any other bitCount is a lowering error, also when a ref overrides it.
- Codec values are numbers. The float's bits go in stream order like an integer's, so its sign bit comes first and each word follows `layout.byteorder`. A value too big for a 32-bit float is a codec error. A `<default>` that is too big is a `default-out-of-range` error.

### Raw data
- `<hexdata bitCount="64">` is fixed raw data (`Bytes`): bitCount/8 bytes, and bitCount must be whole bytes. `<hexdata bitCount="variable" countVariable="../numBytes">` takes its byte count from an earlier integer field, a path relative to the field like an array's `countValue`. Validation checks that the path resolves, comes first and is an integer.
- Codec values are `bytes` (a `bytearray` is accepted), in stream order. A fixed field needs exactly its size. A variable one fills its count field from the length, and a conflicting count is an error.

### Strings
- `<string bitCount="64">` is fixed: bitCount/8 one-byte characters, zero-filled when shorter, no terminator when full. bitCount must be whole words (`string-bits`).
- `<string bitCount="variable">` is an 8-bit length then that many characters (0–255), with no terminator and no alignment: 8–2048 bits.
- Codec values are `str`, one Latin-1 byte per character. Characters go in stream order, so the first is in the high byte of its word.

### Default cases
- `discrimValue="default"` marks the case used when the discriminator matches no other one (`Case.value is DEFAULT`). Usually it's written `<spare discrimValue="default" bitCount="30"/>`, a case with no fields that is named `default`. Any case can be the default, though. A `<spare>` case with a number is a fieldless case named `spare<N>`.
- Decoding falls back to the default case. Encoding can't pick the discriminator for it: the value comes from the caller or the field's `<default>`, and it mustn't be another case's value. Expansion gives it a variant (`command/default`) whose discriminator stays open (no const). Two default cases in one union are a `duplicate-case-value`.
- A `<spare>` without `discrimValue` is taken as the default case when it is the union's only `<spare>`, with a `spare-without-discrim-value` warning. Next to other spares it is an error, as before.

### Named values and defaults
- `<namedValues restrict="true">` with `<namedValue name value>` children gives an integer its names; `restrict` means only those values are legal, which codecs check.
- `<namedValues fromUnion="CommandBodyUnion">` adds the cases of that union (name → discrimValue), ahead of any listed ones. The IR keeps the union name. Lowering fills the values in per message, looking for a union of that name in the groups enclosing the field, innermost first, so a shared header fieldDef gets each message's own union. Only the path down to the field is copied. A name that matches nothing is a `from-union-not-found` warning.
- `<namedValues fromUnion="#CmdUnion">` (or `#alias:id`) takes the cases of the union **fieldDef** with that id instead, resolved when lowering. An unknown id, or a fieldDef that isn't a union, is a `from-union-not-found` warning.
- `<namedValues fromUnion="//Command/CommandBody">` names a union by its absolute path: a message of the same ICD, then the path inside it (union cases are a level, as in relative paths). `//alias:Command/...` reaches an imported ICD. Like a name, it's filled in once the message is lowered, so a field may name a union in its own message. A missing message or field, or a field that isn't a union, is a `from-union-not-found` warning.
- Dialects can lower a tag to a union that takes its cases from an absolute path (`Lowering.cases_from`). The example dialect's `<apCommand>` is one: the cases of `//Command/CommandBody`, keyed by `../CommandNumber` unless it gives a `discrimField`. Since the IR is a tree, an `<apCommand>` met again inside those cases (a command that stores a command) is left with no cases: stored commands are one level deep. A missing target is a lowering error.
- Unions inside `<fieldDefs>` may leave out `discrimField`. The use gives it (`<union name="body" ref="CmdUnion" discrimField="../id"/>`), and a union with none is a `missing-discrim` error. A union written directly in a message still requires it.
- `<default>` (a number, or an integer's named value) is used by encoders when a field is left out. A chosen case still sets its discriminator, and a given value wins. A default may be outside the restricted values (e.g. a "none" 0), but it must fit in the bits (`default-out-of-range`).
- A ref can override `<namedValues>` and `<default>`, as with `<label>`/`<description>`.

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
A variant is a message with some unions resolved: each chosen union is replaced by its case and the case's discriminator is fixed. Variants feed doc tables, code generation, coverage listings and diffs, which want complete messages with small context and no unions.

- **Which unions:** the dialect lists discriminator paths, written from the top-level message in the original (unexpanded) message, `*` matching one level:
  ```python
  EXAMPLE = Dialect(..., expand=(
      "command/header/id", "return/header/id",      # command / return bodies
      "command/commandBody/*/subid",                 # subcommands
      "return/returnBody/*/systemId",                # system IDs (example dialect)
  ))
  ```
  A union is split when its discriminator's path matches. Subcommands and system IDs are the same mechanism; a system ID's value is a tuple. A plain `systemId` field with no union keyed on it is not split. A pattern applies to every loaded ICD; an alias prefix (`payload:command/...`) limits it to one.
- **Replacing a union:** the case's fields go in a group named after the union (`command/power` → `commandBody/{systemId, mode, spare}`); an unnamed union's fields go straight into the parent. The discriminator stays in the layout, fixed: `Message.consts` maps its path in the variant to the value. System ID levels stand alone: `fuel.tanks` gets only its own fields, not `fuel`'s.
- **Nesting:** expansion repeats inside each variant (`command/id=5/subid=10`). Two splittable unions side by side nest in field order, giving every combination.
- **Not expanded:**
  - unions whose discriminator isn't listed (and anything inside them);
  - unions inside an array element, since each element may choose differently (`expand-in-array` error when listed);
  - carried messages: the variant links to the payload ICD's own tree.
- **Identity:** `id` = message name, then the discriminator values in order: `("return", 1, (10, 1, 0))`. `name` = case names joined by `/`: `return/status/fuel.tanks`. Diffs match variants by `id`.
- **Output:** a tree per message. Each node is a variant (the root is the message itself); leaves are fully expanded.
- **Errors first:** expansion refuses an ICD with validation errors. Duplicate case values would give two variants one id.
- A listed pattern that matches no union is a warning.

### HTML viewer
- `reed html FILES --dialect example -o icd.html [--title T]` writes one self-contained page (no server, no network): the left side is each ICD's variant tree with a filter box; the right side shows the chosen variant's size, id, fixed discriminators, the field table and a word-by-word bit map. The URL hash names the variant (`#example:Command/power`), so links can be shared.
- The field table's columns are word, field, bits, type and description:
  - **word**: the 16-bit word number from 0 (`2–3` for a field that spans words); fields in the same word share the cell, except across union cases;
  - **field**: groups, unions, cases and arrays are columns spanning their fields' rows, with vertical labels (`power = 1`, `CommandBody union on ../header/CommandNumber`, `batteries × ../count`) that stay pinned at the top while their rows scroll past. A group with one field has no room for a vertical label, so the field's name carries it as a caption;
  - **bits**: numbered MSB first, 15 to 0 (`15–12`; `1–11` for a field that runs from bit 1 of one word into the next);
  - **type**: the type, or for a field with named values the value-to-name mapping, with the value fixed in this variant highlighted;
  - **description**: label, description, default.
- `reed.html.field_rows(message, icds)` builds the rows: each field's offset, `word` and `bit_span` labels (`word_span()`: `…` for an unknown end, `—` for an unknown offset or no bits, `+` for offsets relative to an array element), size, type, named values, default, docs and fixed value. A system ID's fixed tuple is shown on its group and split over its fields. A union that wasn't split lists every case from the same offset; a carried message is one row linking to its own tree. The last word's implicit spare is a `(pad)` row.
- The sidebar is a tree: each ICD, then each message and variant with children, opens and closes with its chevron. Clicking a name shows it and opens it; clicking the name of the item already shown closes it. Above it, an Auto / Light / Dark switch (saved in the browser's localStorage as `reed-theme`) picks the colour scheme; the palette is paper-ui's.
- The page uses IBM Plex Sans and Mono (400, 500, 600; Mono 400, 600), embedded as data URIs so the page needs no network (about 400 KB). The fonts are in `reed/fonts/` under the SIL Open Font License 1.1 (`reed/fonts/OFL.txt`).
- Variants and links are keyed `alias:name` (the namespace when there's no alias). The page template is `reed/viewer.html`; the data goes in as JSON.

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
