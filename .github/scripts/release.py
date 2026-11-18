#!/usr/bin/env python3
"""Semver release helper driven by conventional commits.

Usage (run from the repository root):
  release.py tag                       print version/sha/previous when manifest.json's version is untagged
  release.py current                   print manifest.json's version and the highest lower vA.B.C tag
  release.py prepare --body-file PATH  bump versions in the working tree and write the release PR body
Standard library only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache

MARKETPLACE = ".claude-plugin/marketplace.json"
MANIFEST = "manifest.json"
BREAKING_SUBJECT_RE = re.compile(r"^\w+(\([^)]*\))?!:")
BREAKING_BODY_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.M)
RELEASE_SUBJECT_RE = re.compile(r"chore: release v\d+\.\d+\.\d+")
THROUGH_RE = re.compile(r"^Released-through:[ \t]*([0-9a-f]{40})[ \t]*$", re.M)
LEVELS = {"patch": 0, "minor": 1, "major": 2}


@dataclass
class Commit:
    sha: str
    subject: str
    body: str
    paths: list[str]


@dataclass
class Release:
    version: str
    plugins: dict[str, str]
    through: str


def parse_semver(v):
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", str(v))
    return tuple(int(x) for x in m.groups()) if m else None


def classify(subject, body):
    if BREAKING_SUBJECT_RE.match(subject) or BREAKING_BODY_RE.search(body):
        return "major"
    if re.match(r"^feat(\([^)]*\))?:", subject):
        return "minor"
    return "patch"


def bump(version, level):
    major, minor, patch = parse_semver(version)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def highest_level(commits):
    return max((classify(c.subject, c.body) for c in commits), key=LEVELS.__getitem__, default="patch")


def latest_release_tag(tags):
    found = [(parse_semver(t[1:]), t) for t in tags if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", t)]
    return max(found)[1] if found else None


def released_through(body):
    m = THROUGH_RE.search(body)
    return m.group(1) if m else None


def plan_release(pending, repo_version, plugins, through):
    changed = {}
    for d, (version, commits) in plugins.items():
        if version is None:
            changed[d] = "1.0.0"
        elif commits and parse_semver(version):
            changed[d] = bump(version, highest_level(commits))
    plugin_commits = [c for _, commits in plugins.values() for c in commits]
    if not pending and not plugin_commits and not changed:
        return None
    return Release(bump(repo_version, highest_level(pending + plugin_commits)), changed, through)


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def read_version(path):
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError:
            return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def top_level_version_span(text):
    """Span of the root object's "version" value in valid JSON text, or None."""
    decoder = json.JSONDecoder()
    # expect_key is true only where a depth-1 object key may start
    depth, i, expect_key = 0, 0, False
    while i < len(text):
        ch = text[i]
        if ch == '"':
            key, end = decoder.raw_decode(text, i)
            if depth == 1 and expect_key:
                expect_key = False
                if key == "version":
                    colon = text.index(":", end)
                    value_start = colon + 1 + len(text[colon + 1:]) - len(text[colon + 1:].lstrip())
                    return value_start, decoder.raw_decode(text, value_start)[1]
            i = end
            continue
        if ch in "{[":
            depth += 1
            expect_key = ch == "{" and depth == 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 1:
            expect_key = True
        i += 1
    return None


def write_version(path, version):
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    try:
        root = json.loads(text)
    except ValueError:
        root = None
    if not isinstance(root, dict):
        sys.exit(f"{path}: not a JSON object")
    span = top_level_version_span(text)
    if span:
        text = text[:span[0]] + json.dumps(version) + text[span[1]:]
    else:
        m = re.match(r"\s*\{(\s*)(\S)", text)
        comma = "" if m.group(2) == "}" else ","
        text = text[:m.start(2)] + f'"version": "{version}"{comma}' + m.group(1) + text[m.start(2):]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def plugin_dirs():
    with open(MARKETPLACE, encoding="utf-8") as f:
        data = json.load(f)
    return [os.path.normpath(p["source"]) for p in data.get("plugins", []) if isinstance(p.get("source"), str)]


def plugin_json(d):
    return f"{d}/.claude-plugin/plugin.json"


@lru_cache(maxsize=None)
def read_commit(sha):
    out = git("show", "-s", "--format=%s%x00%b", sha).stdout
    subject, _, body = out.partition("\0")
    # -m with --first-parent keeps merge commits from reporting no paths at all
    raw = git("diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "--root",
              "-m", "--first-parent", "-z", sha).stdout
    return Commit(sha, subject.strip(), body, [p for p in raw.split("\0") if p])


def is_release_commit(c):
    return bool(RELEASE_SUBJECT_RE.fullmatch(c.subject)) and released_through(c.body) is not None


def version_at(sha, path):
    r = git("show", f"{sha}:{path}", check=False)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    return data.get("version") if isinstance(data, dict) else None


def version_setter(path):
    for sha in git("log", "--format=%H", "--", path).stdout.split():
        parent = git("rev-parse", "--verify", "-q", f"{sha}^", check=False).stdout.strip()
        if version_at(sha, path) != (version_at(parent, path) if parent else None):
            return sha
    return None


def anchor_sha(version):
    out = version_setter(MANIFEST)
    if not out:
        sys.exit(f"cannot find the commit that set {MANIFEST} version {version}")
    return out


def commit_body(sha):
    return git("show", "-s", "--format=%b", sha).stdout


def range_start(setter):
    through = released_through(commit_body(setter))
    return through if through and is_release_commit(read_commit(setter)) else setter


def is_ancestor(a, b):
    return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def pending_commits(anchor, version):
    start = range_start(anchor)
    if start == anchor:
        tagged = git("rev-parse", "--verify", "-q", f"refs/tags/v{version}^{{commit}}", check=False).stdout.strip()
        if tagged and is_ancestor(anchor, tagged) and is_ancestor(tagged, "HEAD"):
            start = tagged
    shas = git("rev-list", "--reverse", f"{start}..HEAD").stdout.split()
    return [c for c in (read_commit(s) for s in shas) if not is_release_commit(c)]


def newest_non_release_sha():
    for sha in git("rev-list", "HEAD").stdout.split():
        if not is_release_commit(read_commit(sha)):
            return sha
    sys.exit("no non-release commit on HEAD")


def eligible_commits(d):
    setter = version_setter(plugin_json(d))
    start = range_start(setter) if setter else None
    shas = git("rev-list", "--reverse", f"{start}..HEAD" if start else "HEAD").stdout.split()
    return [c for c in (read_commit(s) for s in shas)
            if not is_release_commit(c) and any(p.startswith(d + "/") for p in c.paths)]


def manifest_version():
    v = read_version(MANIFEST)
    if v is None or parse_semver(v) is None:
        sys.exit(f"{MANIFEST}: version missing or not X.Y.Z")
    return v


def cmd_current():
    version = manifest_version()
    lower = [t for t in git("tag", "--list").stdout.split()
             if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", t) and parse_semver(t[1:]) < parse_semver(version)]
    print(f"version={version}")
    print(f"previous={latest_release_tag(lower) or ''}")
    return 0


def cmd_tag():
    version = manifest_version()
    tags = git("tag", "--list").stdout.split()
    if f"v{version}" in tags:
        return 0
    previous = latest_release_tag(tags)
    if previous and parse_semver(version) <= parse_semver(previous[1:]):
        print(f"{MANIFEST} version {version} is not higher than existing tag {previous}", file=sys.stderr)
        return 1
    print(f"version={version}")
    print(f"sha={anchor_sha(version)}")
    print(f"previous={previous or ''}")
    return 0


def render_body(release, commits, old_plugins):
    lines = [f"Release v{release.version}", "", "Commits:"]
    lines += [f"- {c.subject} (`{c.sha[:7]}`)" for c in commits]
    if release.plugins:
        lines += ["", "Plugins:"]
        for d, new in release.plugins.items():
            old = old_plugins[d] or "unversioned"
            lines.append(f"- {os.path.basename(d)}: {old} -> {new}")
    return "\n".join(lines) + "\n"


def cmd_prepare(body_file):
    repo_version = manifest_version()
    highest = latest_release_tag(git("tag", "--list").stdout.split())
    if highest and parse_semver(repo_version) < parse_semver(highest[1:]):
        print(f"{MANIFEST} version {repo_version} is lower than existing tag {highest}", file=sys.stderr)
        return 1
    pending = pending_commits(anchor_sha(repo_version), repo_version)
    dirs = plugin_dirs()
    versions = {d: read_version(plugin_json(d)) for d in dirs}
    eligible = {d: eligible_commits(d) for d in dirs}
    plan = plan_release(pending, repo_version, {d: (versions[d], eligible[d]) for d in dirs},
                        newest_non_release_sha())
    if plan is None:
        return 0
    seen = {c.sha: c for c in pending}
    for commits in eligible.values():
        seen.update((c.sha, c) for c in commits)
    order = {sha: i for i, sha in enumerate(git("rev-list", "--reverse", "HEAD").stdout.split())}
    listed = sorted(seen.values(), key=lambda c: order[c.sha])
    write_version(MANIFEST, plan.version)
    for d, v in plan.plugins.items():
        write_version(plugin_json(d), v)
    with open(body_file, "w", encoding="utf-8") as f:
        f.write(render_body(plan, listed, versions))
    print(f"version={plan.version}")
    print(f"through={plan.through}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tag")
    sub.add_parser("current")
    prep = sub.add_parser("prepare")
    prep.add_argument("--body-file", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        return cmd_prepare(args.body_file)
    return cmd_tag() if args.cmd == "tag" else cmd_current()


if __name__ == "__main__":
    sys.exit(main())
