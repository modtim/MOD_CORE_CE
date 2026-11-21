#!/usr/bin/env python3
"""OZ-G1 — `modcore`: thin themed-verb dispatcher for MOD Core CLI tools.

Verbs take the Oz codenames from the internal design notes
(e.g. `init toto` = Workflow Builder). The dispatcher only RESOLVES names
through product_names.py; it never renames an internal identifier, env var,
route, compose service or module path.

Usage:
    python -m tools.modcore init toto
    python -m tools.modcore --help

An unknown verb gets argparse's standard error (exit 2), i.e. today's text.
Display of the codename follows the MOD_THEME_NAMES flag (off = internal key).
"""

from __future__ import annotations

import argparse
import sys

from product_names import PRODUCT_NAMES, theme_enabled


def resolve_product(name: str | None) -> str | None:
    """Return the internal product key for a codename (case-insensitive).

    The internal key itself is accepted too, so today's names keep working.
    Unknown or empty input -> None.
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    if wanted in PRODUCT_NAMES:
        return wanted
    for key, entry in PRODUCT_NAMES.items():
        if wanted == str(entry.get("codename", "")).lower():
            return key
    return None


def cmd_init(args: argparse.Namespace) -> int:
    """`modcore init <codename>`: resolve the surface and report it.

    No state is changed here: the verb is an alias layer over today's init.
    """
    key = resolve_product(args.product)
    if key is None:
        print(
            f"modcore: unknown product '{args.product}' "
            "(use a codename from NAME-MAP, e.g. toto)",
            file=sys.stderr,
        )
        return 1
    shown = PRODUCT_NAMES[key]["codename"] if theme_enabled() else key
    print(f"modcore init: {shown} -> surface '{key}'")
    print("modcore init: alias only; no state changed")
    return 0


def cmd_names(args: argparse.Namespace) -> int:
    """`modcore names`: print the NAME-MAP read from product_names.py.

    The map is read at RUN TIME (never hard-coded here), so a concurrent key
    rename needs no change to this verb. Flag ON = codename + label +
    subtitle; flag OFF (MOD_THEME_NAMES) = today's names: the internal keys,
    byte for byte. Product ids, module paths and env names never change.
    """
    on = theme_enabled()
    print(f"modcore names: {len(PRODUCT_NAMES)} products (NAME-MAP via product_names.py)")
    for key, entry in PRODUCT_NAMES.items():
        if on:
            codename = str(entry.get("codename", ""))
            label = str(entry.get("label", ""))
            subtitle = str(entry.get("subtitle", ""))
            print(f"{key}: {codename} - {label} ({subtitle})")
        else:
            print(f"{key}: {key}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modcore",
        description="MOD Core themed CLI verbs (codenames from NAME-MAP).",
    )
    sub = parser.add_subparsers(dest="verb", required=True, metavar="VERB")
    init_p = sub.add_parser(
        "init",
        help="Initialise a product surface by codename (e.g. init toto).",
    )
    init_p.add_argument(
        "product",
        help="Product codename, e.g. toto (Workflow Builder).",
    )
    init_p.set_defaults(func=cmd_init)
    names_p = sub.add_parser(
        "names",
        help="Print the NAME-MAP from product_names.py (flag off = today's names).",
    )
    names_p.set_defaults(func=cmd_names)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
