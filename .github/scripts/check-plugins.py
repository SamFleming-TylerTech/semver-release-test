#!/usr/bin/env python3
"""PR checks for plugins: versions are semver and not lowered, and SKILL.md/agent frontmatter.

Versions in manifest.json and each plugin.json are optional; a present one must be X.Y.Z and
not lower than on <base-ref>. Releases bump them automatically, hand edits are allowed.

Usage: check-plugins.py <base-ref>     (e.g. origin/main)
Run from the repository root. Requires PyYAML.
"""
import json
import os
import re
import subprocess
import sys

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    print("Install PyYAML: python3 -m pip install pyyaml==6.0.2", file=sys.stderr)
    sys.exit(1)

MARKETPLACE = ".claude-plugin/marketplace.json"
MANIFEST = "manifest.json"
errors = []


def error(file, msg):
    errors.append(msg)
    print(f"::error file={file}::{msg}")


def git_show(ref, path):
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def parse_semver(v):
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", str(v))  # must match release.py exactly
    return tuple(int(x) for x in m.groups()) if m else None


def plugin_dirs():
    with open(MARKETPLACE) as f:
        data = json.load(f)
    for p in data.get("plugins", []):
        src = p.get("source")
        if isinstance(src, str):
            yield p.get("name", src), os.path.normpath(src)


def check_file_removed(name, path, base):
    old_raw = git_show(base, path)
    if old_raw is None:
        return
    try:
        old_data = json.loads(old_raw)
    except ValueError:
        return
    old_v = old_data.get("version") if isinstance(old_data, dict) else None
    if old_v is not None and parse_semver(old_v) is not None:
        error(path, f"{name}: file removed but base has version {old_v}")


def check_file_version(name, path, base):
    try:
        with open(path) as f:
            new_data = json.load(f)
    except OSError:
        check_file_removed(name, path, base)
        return
    except ValueError as e:
        error(path, f"{name}: cannot read {path}: {e}")
        return
    if not isinstance(new_data, dict):
        error(path, f"{name}: JSON root must be an object")
        return
    new_v = new_data.get("version")
    new_t = None
    if new_v is not None:
        new_t = parse_semver(new_v)
        if new_t is None:
            error(path, f"{name}: version '{new_v}' is not semver (X.Y.Z)")
            return
    old_raw = git_show(base, path)
    if old_raw is None:
        return
    try:
        old_data = json.loads(old_raw)
    except ValueError:
        return
    if not isinstance(old_data, dict):
        error(path, f"{name}: JSON root must be an object (on base)")
        return
    old_v = old_data.get("version")
    old_t = parse_semver(old_v) if old_v is not None else None
    if new_t is None:
        if old_t is not None:
            error(path, f"{name}: version must not be removed (base {old_v})")
        return
    if old_t is not None and new_t < old_t:
        error(path, f"{name}: version must not be lower than base (base {old_v}, HEAD {new_v})")


def check_versions(base):
    check_file_version("manifest.json", MANIFEST, base)
    for name, d in plugin_dirs():
        check_file_version(name, f"{d}/.claude-plugin/plugin.json", base)


def frontmatter(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text + "\n", re.S)
    if not m:
        error(path, "missing YAML frontmatter (file must start with '---')")
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        error(path, f"invalid YAML frontmatter: {str(e).splitlines()[0]}")
        return None
    if not isinstance(data, dict):
        error(path, "frontmatter is not a mapping")
        return None
    return data


def nonempty(v):
    return isinstance(v, str) and v.strip() != ""


def check_frontmatter():
    for _, d in plugin_dirs():
        sdir = os.path.join(d, "skills")
        if os.path.isdir(sdir):
            for skill in sorted(os.listdir(sdir)):
                if skill.startswith(".") or not os.path.isdir(os.path.join(sdir, skill)):
                    continue
                path = os.path.join(sdir, skill, "SKILL.md")
                if not os.path.isfile(path):
                    error(os.path.join(sdir, skill), f"skill folder '{skill}' has no SKILL.md (Claude Code will not load it)")
                    continue
                fm = frontmatter(path)
                if fm is None:
                    continue
                if fm.get("name") != skill:
                    error(path, f"skill name '{fm.get('name')}' must equal folder name '{skill}'")
                if not nonempty(fm.get("description")):
                    error(path, "skill frontmatter needs a non-empty string 'description'")
        adir = os.path.join(d, "agents")
        if os.path.isdir(adir):
            for fn in sorted(os.listdir(adir)):
                if not fn.endswith(".md"):
                    continue
                path = os.path.join(adir, fn)
                fm = frontmatter(path)
                if fm is None:
                    continue
                for key in ("name", "description"):
                    if not nonempty(fm.get(key)):
                        error(path, f"agent frontmatter needs a non-empty string '{key}'")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    check_versions(sys.argv[1])
    check_frontmatter()
    if errors:
        print(f"{len(errors)} problem(s) found")
        return 1
    print("All plugin checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
