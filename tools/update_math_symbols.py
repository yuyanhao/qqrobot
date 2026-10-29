"""Snapshot symbols from the preloaded LaTeX/AMS packages; optionally merge them."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT
SOURCES = ("fontmath.ltx", "amsfonts.sty", "amssymb.sty")
# Public math macros whose definitions are not DeclareMathSymbol declarations.
# These are explicit, reviewed names; never collect arbitrary macro definitions.
EXTRA = {name: "\\" + name for name in """
doteq bowtie hookrightarrow hookleftarrow longmapsto surd dashrightarrow dashleftarrow dasharrow Join
coth hom injlim projlim varinjlim varprojlim varliminf varlimsup dotsb dotsc dotsm dotsi dotso
""".split()}
EXTRA.update({name: "\\" + name + "{x}" for name in """
xrightarrow xleftarrow overrightarrow overleftarrow overleftrightarrow underrightarrow underleftarrow underleftrightarrow
mathop mathrel mathbin mathord mathopen mathclose mathpunct mathinner
""".split()})


def catalogue():
    kpsewhich = shutil.which("kpsewhich")
    if not kpsewhich:
        raise SystemExit("kpsewhich unavailable; install TeX Live before regenerating the catalogue")
    commands, aliases, sources = {}, [], []
    for name in SOURCES:
        path = Path(subprocess.check_output([kpsewhich, name], text=True).strip())
        data = path.read_bytes()
        text = re.sub(r"(?<!\\)%[^\n]*", "", data.decode("utf-8"))
        sources.append({"file": name, "sha256": hashlib.sha256(data).hexdigest()})
        # Only explicit braced symbol names are data. Unbraced occurrences can
        # be arguments to a macro definition, rather than symbol declarations.
        for kind, name in re.findall(
                r"\\(?:ams@)?DeclareMath(Symbol|Delimiter|Accent)\s*\{\s*\\([A-Za-z]+)\s*\}", text):
            commands[name] = kind.lower()
        aliases.extend(re.findall(r"\\let\s*\\([A-Za-z]+)\s*=?\s*\\([A-Za-z]+)(?![A-Za-z@])", text))
    changed = True
    while changed:
        changed = False
        for name, target in aliases:
            if name not in commands and target in commands:
                commands[name] = commands[target]
                changed = True
    forbidden = {"def", "edef", "input", "include", "write", "read", "openin", "openout", "csname",
                 "newcommand", "renewcommand", "catcode", "special", "directlua", "usepackage"}
    if commands.keys() & forbidden:
        raise ValueError("The symbol snapshot unexpectedly includes a programming or file interface")
    return {"sources": sources, "commands": dict(sorted(commands.items())), "extra_examples": EXTRA}


def merge(names):
    path = APP / "render.py"
    source = path.read_text(encoding="utf-8")
    assignment = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "_MATH_COMMANDS" for target in node.targets))
    literal = next(node for node in ast.walk(assignment.value)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str))
    missing = sorted(set(names) - set(literal.value.split()))
    if missing:
        lines = source.splitlines(keepends=True)
        insertion = "\n".join(textwrap.wrap(" ".join(missing), width=95)) + "\n"
        lines.insert(literal.end_lineno - 1, insertion)
        updated = "".join(lines)
        compile(updated, str(path), "exec")
        path.write_text(updated, encoding="utf-8", newline="\n")
    return missing


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = catalogue()
    (APP / "tests" / "fixtures" / "standard-math-symbols.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Catalogued {len(data['commands'])} standard mathematical symbols/accents/delimiters and their aliases.")
    if args.apply:
        missing = merge(set(data["commands"]) | set(data["extra_examples"]))
        print(f"Added {len(missing)} missing names: {' '.join(missing)}")
