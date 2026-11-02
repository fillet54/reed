# reed

Reads XML ICD definitions into a normalized message model, and builds tools on it:
- codecs: Python dicts ⇄ packed binary messages;
- table views of message layouts (docutils/Sphinx, HTML, text);
- expansion of messages into concrete variants (per command/return, per system ID);
- a browsable HTML page of every variant (`reed html`) and a diff of two versions, classed editorial, compatible or incompatible (`reed diff`).

Design: [docs/architecture.md](docs/architecture.md). Succeeds the `xmlicd` prototype.

```sh
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```
