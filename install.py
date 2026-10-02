#!/usr/bin/env python3
"""Install gemini-normaliser into a project.

    python3 install.py PROJECT_DIR [--protect PATH ...]   install or update
    python3 install.py PROJECT_DIR --check                 report only, change nothing
    python3 install.py PROJECT_DIR --uninstall             remove hooks and guard files

Writes, inside PROJECT_DIR only:
    .gemini/guard/{gemini_guard.py,contract.md,test_gemini_guard.py}
    .agents/hooks.json      Antigravity hooks (merged under the "gemini-normaliser" key)
    .gemini/settings.json   Gemini CLI hooks (merged; other settings and hooks kept)
    .gemini/guard.json      per-project config (created once, never overwritten)
    GEMINI.md               contract import line added (file created if missing)
    .gitignore              .gemini/tmp/ added
Stdlib only, Python 3.9+.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD_FILES = ("gemini_guard.py", "contract.md", "test_gemini_guard.py")
HOOK_KEY = "gemini-normaliser"
MARK = "gemini_guard.py"
IMPORT_AGY = "@[gemini working contract](.gemini/guard/contract.md)"
RULE_CAP = 24000  # Antigravity keeps only the first 24,000 bytes of each rule file


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as e:
        sys.exit("refusing to touch %s: not valid JSON (%s)" % (path, e))


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def template(*parts):
    return load_json(os.path.join(HERE, "templates", *parts), {})


def strip_ours(entries):
    """Drop hook groups whose commands run gemini_guard.py; keep everyone else's."""
    return [e for e in entries if MARK not in json.dumps(e)]


def merge_cli_settings(path, remove=False):
    data = load_json(path, {})
    hooks = data.setdefault("hooks", {})
    for event in list(hooks):
        hooks[event] = strip_ours(hooks[event])
        if not hooks[event]:
            del hooks[event]
    if not remove:
        for event, entries in template(".gemini", "settings.json")["hooks"].items():
            hooks.setdefault(event, []).extend(entries)
    if not hooks:
        data.pop("hooks")
    if data or os.path.exists(path):
        write_json(path, data)


def merge_agy_hooks(path, remove=False):
    data = load_json(path, {})
    data = {k: v for k, v in data.items() if MARK not in json.dumps(v)}  # also clears old key names
    if not remove:
        data[HOOK_KEY] = template(".agents", "hooks.json")[HOOK_KEY]
    if data or os.path.exists(path):
        write_json(path, data)


def ensure_line(path, line, header=None):
    text = open(path).read() if os.path.exists(path) else ""
    if line in text.splitlines():
        return False
    with open(path, "a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        if header:
            f.write(("\n" if text else "") + header + "\n")
        f.write(line + "\n")
    return True


def remove_line(path, line):
    if not os.path.exists(path):
        return
    lines = open(path).read().splitlines()
    if line in lines:
        with open(path, "w") as f:
            f.write("\n".join(l for l in lines if l != line).rstrip("\n") + "\n")


def check(project):
    notes = []
    for name in ("AGENTS.md", "GEMINI.md"):
        p = os.path.join(project, name)
        if os.path.exists(p):
            size = os.path.getsize(p)
            flag = "  <-- over Antigravity's 24 KB cap; the end is cut" if size > RULE_CAP else ""
            notes.append("%-10s %6d bytes%s" % (name, size, flag))
    for rel in (".agents/hooks.json", ".gemini/settings.json", ".gemini/guard/gemini_guard.py"):
        p = os.path.join(project, rel)
        ok = os.path.exists(p) and (rel.endswith(".py") or MARK in open(p).read())
        notes.append("%-30s %s" % (rel, "installed" if ok else "missing"))
    return notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--protect", action="append", default=[], metavar="PATH",
                    help="append-only path relative to the project (repeatable; only on first install)")
    ap.add_argument("--check", action="store_true", help="report status and rule-file sizes only")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args(argv)
    project = os.path.abspath(a.project)
    if not os.path.isdir(project):
        sys.exit("not a directory: %s" % project)

    if a.check:
        print("\n".join(check(project)))
        return 0

    guard = os.path.join(project, ".gemini", "guard")
    if a.uninstall:
        merge_agy_hooks(os.path.join(project, ".agents", "hooks.json"), remove=True)
        merge_cli_settings(os.path.join(project, ".gemini", "settings.json"), remove=True)
        remove_line(os.path.join(project, "GEMINI.md"), IMPORT_AGY)
        if os.path.isdir(guard):
            shutil.rmtree(guard)
        print("removed hooks, guard files and the GEMINI.md import. kept .gemini/guard.json and .gemini/tmp/.")
        return 0

    os.makedirs(guard, exist_ok=True)
    for name in GUARD_FILES:
        shutil.copy2(os.path.join(HERE, "guard", name), os.path.join(guard, name))
    merge_agy_hooks(os.path.join(project, ".agents", "hooks.json"))
    merge_cli_settings(os.path.join(project, ".gemini", "settings.json"))
    cfg = os.path.join(project, ".gemini", "guard.json")
    if not os.path.exists(cfg):
        write_json(cfg, {"protected_paths": a.protect})
    elif a.protect:
        print("note: .gemini/guard.json exists; edit protected_paths there (--protect ignored)")
    gemini_md = os.path.join(project, "GEMINI.md")
    if not os.path.exists(gemini_md) and os.path.exists(os.path.join(project, "AGENTS.md")):
        ensure_line(gemini_md, "@./AGENTS.md")  # Gemini CLI does not read AGENTS.md on its own
    ensure_line(gemini_md, IMPORT_AGY)
    if os.path.isdir(os.path.join(project, ".git")):
        ensure_line(os.path.join(project, ".gitignore"), ".gemini/tmp/", header="# gemini-normaliser session state")
    print("installed into %s" % project)
    print("\n".join(check(project)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
