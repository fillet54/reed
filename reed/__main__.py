"""Command line: `reed check FILE...`, `reed expand FILE...`, `reed html FILE...` and
`reed diff OLD NEW`."""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

from . import dialects
from .expand import ExpandError, Variant, expand
from .ir import Union
from .lower import LoweringError
from .size import describe_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reed")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="lower and validate ICD files")
    check.add_argument("files", nargs="+", help="ICD XML files (imports are found next to them)")
    check.add_argument("--dialect", choices=dialects.NAMES, default="base")
    check.add_argument("--sizes", action="store_true", help="print message and case sizes")
    check.add_argument("--stats", action="store_true", help="print time and peak memory")

    tree = commands.add_parser("expand", help="print the variant tree of each message")
    tree.add_argument("files", nargs="+", help="ICD XML files (imports are found next to them)")
    tree.add_argument("--dialect", choices=dialects.NAMES, default="base")

    view = commands.add_parser("html", help="write a browsable page of the expanded messages")
    view.add_argument("files", nargs="+", help="ICD XML files (imports are found next to them)")
    view.add_argument("--dialect", choices=dialects.NAMES, default="base")
    view.add_argument("-o", "--output", default="icd.html", help="page to write (icd.html)")
    view.add_argument("--title", help="page title (the first file's name)")

    changes = commands.add_parser("diff", help="compare two versions of an ICD, variant by variant")
    changes.add_argument("old", help="the old ICD XML file (imports are found next to it)")
    changes.add_argument("new", help="the new ICD XML file")
    changes.add_argument("--dialect", choices=dialects.NAMES, default="base")
    changes.add_argument("-o", "--output", default="diff.html", help="page to write (diff.html)")
    changes.add_argument("--title", help="page title (the new file's name)")

    args = parser.parse_args(argv)
    if args.command == "diff":
        return run_diff(args)
    if args.command == "expand":
        return run_expand(args)
    if args.command == "html":
        return run_html(args)
    return run_check(args)


def run_html(args) -> int:
    from .html import page

    dialect = dialects.get(args.dialect)
    title = args.title or Path(args.files[0]).stem
    try:
        icds = dialect.load(args.files)
        text = page(icds, dialect, title=title)
        Path(args.output).write_text(text, encoding="utf-8")
    except (OSError, LoweringError, ExpandError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"wrote {args.output}")
    return 0


def run_diff(args) -> int:
    from .diff import diff, summary_lines
    from .html import diff_page

    dialect = dialects.get(args.dialect)
    title = args.title or Path(args.new).stem
    try:
        tree = diff(dialect.load([args.old]), dialect.load([args.new]), dialect)
        text = diff_page(tree, dialect, title=title, old=args.old, new=args.new)
        Path(args.output).write_text(text, encoding="utf-8")
    except (OSError, LoweringError, ExpandError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("\n".join(summary_lines(tree)))
    print(f"wrote {args.output}")
    return 0


def run_expand(args) -> int:
    dialect = dialects.get(args.dialect)
    try:
        icds = dialect.load(args.files)
        trees = expand(icds, dialect)
    except (OSError, LoweringError, ExpandError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for roots in trees.values():
        for root in roots:
            print_tree(root, icds)
    return 0


def print_tree(variant: Variant, icds, lead: str = "", branch: str = "") -> None:
    ident = ", ".join(map(str, variant.id))
    print(f"{lead}{branch}{variant.name}  ({ident})  {describe_size(variant.message, icds)}")

    lead += {"": "", "├─ ": "│  ", "└─ ": "   "}[branch]
    for i, child in enumerate(variant.children):
        last = i == len(variant.children) - 1
        print_tree(child, icds, lead, "└─ " if last else "├─ ")


def run_check(args) -> int:
    dialect = dialects.get(args.dialect)
    started = time.perf_counter()

    try:
        icds = dialect.load(args.files)
    except (OSError, LoweringError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    lowered = time.perf_counter()
    issues = dialect.validate(icds)
    validated = time.perf_counter()

    for issue in issues:
        print(issue)

    if args.sizes:
        print_sizes(icds)

    messages = sum(len(icd.messages) for icd in icds.values())
    errors = sum(1 for i in issues if i.severity == "error")
    warnings = len(issues) - errors
    print(
        f"{plural(messages, 'message')}, {plural(errors, 'error')}, "
        f"{plural(warnings, 'warning')}"
    )

    if args.stats:
        peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(
            f"lowered in {lowered - started:.2f}s, validated in {validated - lowered:.2f}s, "
            f"peak memory {peak_mb:.0f} MB"
        )

    return 1 if errors else 0


def print_sizes(icds) -> None:
    for icd in icds.values():
        for message in icd.messages:
            print(f"{message.name}  {describe_size(message, icds)}")
            for field in message.body.fields:
                if isinstance(field, Union):
                    for case in field.cases:
                        size = describe_size(message, icds, choose={id(field): case})
                        print(f"  {case.name}  {size}")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


if __name__ == "__main__":
    sys.exit(main())
