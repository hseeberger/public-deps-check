#!/usr/bin/env python3
"""Require a breaking-change marker when a public dependency gets a semver-incompatible bump.

Subcommands:

- `analyze`: compare a PR head with the merge base of base and head. Fails when a published
  crate bumps a dependency that appears in its public API to an incompatible version and the
  head commit subject has no `!`, or when such a dependency is declared with `workspace = true`.
- `exposed`: list the dependencies that appear in the public API of every published crate.
- `mark`: rewrite the subject of a Dependabot commit from `type(scope): ...` to
  `type(scope)!: ...` through the GitHub API and move the PR branch with a lease.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field

FINDING_NONE = "none"
FINDING_MISSING_MARKER = "missing-marker"
FINDING_WORKSPACE_TRUE = "workspace-true"
FINDING_UNDECIDED = "undecided"
FINDING_ERROR = "error"

STD_CRATES = {"std", "core", "alloc", "proc_macro", "test"}

MARKER_SUBJECT = re.compile(r"^[A-Za-z]+(\([^)]*\))?!:")
MARKER_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
PLAIN_CARET = re.compile(r"^\^?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
SUBJECT = re.compile(r"^(?P<prefix>[A-Za-z]+(\([^)]*\))?)(?P<bang>!?):(?P<rest>.*)$", re.DOTALL)


class Undecided(Exception):
    pass


@dataclass
class Bump:
    package: str
    dependency: str
    old: str
    new: str


@dataclass
class Exposure:
    dependency: str
    items: list[str] = field(default_factory=list)


@dataclass
class Result:
    finding: str
    head_sha: str
    messages: list[str]


def main() -> int:
    args = parse_args()
    return args.run(args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(required=True)

    analyze = commands.add_parser("analyze", help="check a PR head against its base")
    analyze.add_argument("--repo", default=".", help="git repository with a Cargo project")
    analyze.add_argument("--base", required=True, help="base commit (the PR target)")
    analyze.add_argument("--head", required=True, help="head commit (the PR commit)")
    analyze.add_argument("--toolchain", required=True, help="nightly toolchain for rustdoc JSON")
    analyze.add_argument("--target-dir", help="cargo target directory")
    analyze.set_defaults(run=run_analyze)

    exposed = commands.add_parser("exposed", help="list public dependencies of all crates")
    exposed.add_argument("--repo", default=".", help="Cargo project directory")
    exposed.add_argument("--toolchain", required=True, help="nightly toolchain for rustdoc JSON")
    exposed.add_argument("--target-dir", help="cargo target directory")
    exposed.set_defaults(run=run_exposed)

    mark = commands.add_parser("mark", help="add `!` to the subject of a Dependabot commit")
    mark.add_argument("--repository", required=True, help="owner/name")
    mark.add_argument("--pr", required=True, type=int, help="pull request number")
    mark.add_argument("--head", required=True, help="the analyzed head commit")
    mark.set_defaults(run=run_mark)

    return parser.parse_args()


def run_analyze(args: argparse.Namespace) -> int:
    repo = os.path.abspath(args.repo)
    head_sha = git(repo, "rev-parse", f"{args.head}^{{commit}}")
    try:
        result = analyze(repo, args.base, head_sha, args.toolchain, target_dir(repo, args))
    except Exception as error:
        result = Result(FINDING_ERROR, head_sha, [f"error: {error}"])
    write_outputs(result)
    for message in result.messages:
        print(message)
    print(f"finding: {result.finding}")
    return 0 if result.finding == FINDING_NONE else 1


def run_exposed(args: argparse.Namespace) -> int:
    repo = os.path.abspath(args.repo)
    metadata = cargo_metadata(repo, no_deps=False)
    graph = DependencyGraph(metadata)
    for package in published_libraries(metadata):
        exposures = exposed_dependencies(
            repo, package, graph, args.toolchain, target_dir(repo, args)
        )
        print(package["name"])
        for exposure in sorted(exposures.values(), key=lambda e: e.dependency):
            workspace = " (workspace = true)" if is_workspace_dependency(
                package, exposure.dependency
            ) else ""
            print(f"  {exposure.dependency}{workspace}: {exposure.items[0]}")
    return 0


def run_mark(args: argparse.Namespace) -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GH_TOKEN is not set")
        return 1
    api = GitHub(token, args.repository)
    try:
        branch, new_sha = create_marked_commit(api, args.pr, args.head)
    except Refused as refused:
        print(f"not marking: {refused}")
        return 0
    url = f"https://github.com/{args.repository}.git"
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    print(f"::add-mask::{auth}")
    env = authorized_env(f"Authorization: Basic {auth}")
    with tempfile.TemporaryDirectory() as git_dir:
        git(git_dir, "init", "--quiet", "--bare")
        git(git_dir, "fetch", "--quiet", url, new_sha, env=env)
        pushed = push_with_lease(git_dir, url, branch, args.head, new_sha, env)
    if not pushed:
        print(f"not marking: {branch} has moved away from {args.head}")
        return 0
    print(f"marked: {branch} now at {new_sha}")
    return 0


def analyze(repo: str, base: str, head: str, toolchain: str, target: str) -> Result:
    merge_base = git(repo, "merge-base", base, head)
    changed = git(repo, "diff", "--name-only", merge_base, head).splitlines()
    if not any(os.path.basename(path) == "Cargo.toml" for path in changed):
        return Result(FINDING_NONE, head, ["no Cargo.toml changed"])

    with worktree(repo, merge_base) as old_dir, worktree(repo, head) as new_dir:
        old_metadata = cargo_metadata(old_dir, no_deps=True)
        new_metadata = cargo_metadata(new_dir, no_deps=True)
        try:
            bumps = incompatible_bumps(old_metadata, new_metadata)
        except Undecided as undecided:
            return Result(FINDING_UNDECIDED, head, [f"cannot decide: {undecided}"])
        if not bumps:
            return Result(FINDING_NONE, head, ["no incompatible dependency bump"])

        try:
            messages = breaking_changes(repo, head, old_dir, new_dir, bumps, toolchain, target)
        except Undecided as undecided:
            return Result(FINDING_UNDECIDED, head, [f"cannot decide: {undecided}"])

    workspace_true = [m for kind, m in messages if kind == FINDING_WORKSPACE_TRUE]
    missing_marker = [m for kind, m in messages if kind == FINDING_MISSING_MARKER]
    if workspace_true:
        return Result(FINDING_WORKSPACE_TRUE, head, workspace_true + missing_marker)
    if missing_marker:
        return Result(FINDING_MISSING_MARKER, head, missing_marker)
    if messages:
        return Result(FINDING_NONE, head, ["breaking bumps are marked"])
    return Result(FINDING_NONE, head, ["no public API item changed to an incompatible version"])


def breaking_changes(
    repo: str,
    head: str,
    old_dir: str,
    new_dir: str,
    bumps: list[Bump],
    toolchain: str,
    target: str,
) -> list[tuple[str, str]]:
    """Return the findings, each a kind and a message, for packages with incompatible bumps."""
    old = Snapshot(old_dir, toolchain, target)
    new = Snapshot(new_dir, toolchain, target)
    marked = has_marker(git(repo, "log", "-1", "--format=%B", head))
    findings = []
    for name in sorted({bump.package for bump in bumps}):
        if name not in old.libraries or name not in new.libraries:
            continue
        package_bumps = [bump for bump in bumps if bump.package == name]
        old_items = old.api_items(name)
        new_items = new.api_items(name)
        changes = {}
        for path, dependency, old_versions, new_versions in changed_items(old_items, new_items):
            change = changes.setdefault(dependency, (set(), set(), []))
            change[0].update(old_versions)
            change[1].update(new_versions)
            change[2].append(path)
        for dependency, (old_versions, new_versions, paths) in sorted(changes.items()):
            culprits = [
                bump
                for bump in package_bumps
                if dependency in old.reachable_names(name, bump.dependency)
                or dependency in new.reachable_names(name, bump.dependency)
            ]
            cause = ", ".join(f"{b.dependency} {b.old} -> {b.new}" for b in culprits)
            shown = ", ".join(f"`{p}`" for p in paths[:3])
            more = f" and {len(paths) - 3} more" if len(paths) > 3 else ""
            evidence = (
                f"{name}: {shown}{more} in the public API now come from {dependency} "
                f"{'/'.join(sorted(new_versions))} instead of {'/'.join(sorted(old_versions))}"
                f" (bump: {cause or 'unknown'})"
            )
            workspace = [
                b.dependency
                for b in culprits
                if is_workspace_dependency(new.libraries[name], b.dependency)
            ]
            if workspace:
                findings.append(
                    (
                        FINDING_WORKSPACE_TRUE,
                        f"{evidence}; declare {', '.join(workspace)} in {name}'s own "
                        "Cargo.toml, not with workspace = true",
                    )
                )
            elif marked:
                findings.append((FINDING_NONE, evidence))
            else:
                findings.append(
                    (FINDING_MISSING_MARKER, f"{evidence}; the commit subject needs a `!`")
                )
    return findings


def changed_items(
    old_items: dict[tuple[str, str, str, str], set[tuple[str, str]]],
    new_items: dict[tuple[str, str, str, str], set[tuple[str, str]]],
) -> list[tuple[str, str, set[str], set[str]]]:
    """Return the references public in both snapshots whose defining package changed incompatibly.

    A reference is identified by the local item using it, its position in that item, and the
    canonical path and kind of the foreign item; each maps to its defining packages as
    (name, version).
    """
    changes = []
    for identity in sorted(old_items.keys() & new_items.keys()):
        site, _, path, _ = identity
        names = {name for name, _ in old_items[identity] | new_items[identity]}
        for dependency in sorted(names):
            old_versions = {v for n, v in old_items[identity] if n == dependency}
            new_versions = {v for n, v in new_items[identity] if n == dependency}
            old_keys = {compatibility_key(v) for v in old_versions}
            new_keys = {compatibility_key(v) for v in new_versions}
            if old_versions and new_versions and old_keys != new_keys:
                changes.append((f"{path}` in `{site}", dependency, old_versions, new_versions))
    return changes


class Snapshot:
    """A checked-out commit with its resolve graph and the public API items of its crates."""

    def __init__(self, directory: str, toolchain: str, target: str):
        self.directory = directory
        self.toolchain = toolchain
        self.target = target
        self.metadata = cargo_metadata(directory, no_deps=False)
        self.graph = DependencyGraph(self.metadata)
        self.libraries = {p["name"]: p for p in published_libraries(self.metadata)}

    def api_items(self, name: str) -> dict[tuple[str, str, str, str], set[tuple[str, str]]]:
        """Return the foreign items in a crate's public API with their defining packages."""
        package = self.libraries[name]
        doc, artifacts = rustdoc_json(self.directory, package, self.toolchain, self.target)
        sysroot = rustc_sysroot(self.toolchain)
        items = {}
        for crate_id, references in foreign_reference_sites(doc).items():
            crate = doc["external_crates"].get(str(crate_id))
            if crate is None:
                raise Undecided(f"{name}: rustdoc refers to unknown crate id {crate_id}")
            if crate["name"] in STD_CRATES:
                continue
            path = crate.get("path")
            if path and os.path.realpath(path).startswith(sysroot):
                continue
            package_id = artifacts.get(os.path.realpath(path)) if path else None
            if package_id is None:
                raise Undecided(f"{name}: cannot map crate {crate['name']} ({path}) to a package")
            defining = self.graph.packages[package_id]
            for identity in references:
                items.setdefault(identity, set()).add((defining["name"], defining["version"]))
        return items

    def reachable_names(self, name: str, dependency: str) -> set[str]:
        """Return the package names reachable from a direct dependency of a crate."""
        names = set()
        for package_id in self.graph.direct_dependencies(self.libraries[name]["id"]):
            if self.graph.packages[package_id]["name"] == dependency:
                names.update(self.graph.packages[p]["name"] for p in self.graph.distances(package_id))
        return names


def incompatible_bumps(old_metadata: dict, new_metadata: dict) -> list[Bump]:
    old_packages = {p["name"]: p for p in workspace_packages(old_metadata)}
    members = old_packages.keys() | {p["name"] for p in workspace_packages(new_metadata)}
    bumps = []
    for package in workspace_packages(new_metadata):
        if not is_published(package) or package["name"] not in old_packages:
            continue
        old_reqs = normal_requirements(old_packages[package["name"]])
        new_reqs = normal_requirements(package)
        for dependency, new_req in new_reqs.items():
            old_req = old_reqs.get(dependency)
            if dependency in members or old_req is None or old_req == new_req:
                continue
            if compatibility_key(old_req) != compatibility_key(new_req):
                bumps.append(Bump(package["name"], dependency, old_req, new_req))
    return bumps


def normal_requirements(package: dict) -> dict[str, str]:
    requirements = {}
    for dependency in package["dependencies"]:
        if dependency["kind"] is not None:
            continue
        name = dependency["name"]
        requirement = dependency["req"]
        if requirements.get(name, requirement) != requirement:
            raise Undecided(f"{package['name']} has several requirements for {name}")
        requirements[name] = requirement
    return requirements


def compatibility_key(requirement: str) -> tuple[int, ...]:
    """Return the version parts that Cargo treats as compatible for a caret requirement."""
    match = PLAIN_CARET.match(requirement.strip())
    if match is None:
        raise Undecided(f"requirement `{requirement}` is not a plain caret requirement")
    major, minor, patch = (int(part) if part is not None else None for part in match.groups())
    if major > 0:
        return (major,)
    if minor is None:
        return (0,)
    if minor > 0:
        return (0, minor)
    if patch is None:
        return (0, 0)
    return (0, 0, patch)


def has_marker(message: str) -> bool:
    subject = message.splitlines()[0] if message else ""
    return bool(MARKER_SUBJECT.match(subject) or MARKER_FOOTER.search(message))


class DependencyGraph:
    def __init__(self, metadata: dict):
        self.packages = {p["id"]: p for p in metadata["packages"]}
        self.normal_deps = {}
        for node in metadata["resolve"]["nodes"]:
            self.normal_deps[node["id"]] = [
                dep["pkg"]
                for dep in node["deps"]
                if any(kind["kind"] is None for kind in dep["dep_kinds"])
            ]

    def direct_dependencies(self, package_id: str) -> list[str]:
        """Return the ids of the direct normal dependencies of a package."""
        return self.normal_deps[package_id]

    def distances(self, package_id: str) -> dict[str, int]:
        """Return the packages reachable from a package with their shortest distance."""
        distances = {package_id: 0}
        queue = [package_id]
        for current in queue:
            for dependency in self.normal_deps.get(current, []):
                if dependency not in distances:
                    distances[dependency] = distances[current] + 1
                    queue.append(dependency)
        return distances


def exposed_dependencies(
    directory: str, package: dict, graph: DependencyGraph, toolchain: str, target: str
) -> dict[str, Exposure]:
    """Return the direct dependencies of a package that appear in its public API.

    rustdoc names crates, not packages, so every package with a matching library name is
    considered; with several versions of a crate in the graph, all of their owners count.
    """
    crate_items = foreign_api_items(rustdoc_json(directory, package, toolchain, target)[0])
    direct = graph.direct_dependencies(package["id"])
    distances = {dependency: graph.distances(dependency) for dependency in direct}
    closure = set().union(*distances.values()) if distances else set()
    private = set(((package.get("metadata") or {}).get("public-deps") or {}).get("private", []))

    exposures = {}
    for crate, items in sorted(crate_items.items()):
        candidates = [p for p in closure if library_name(graph.packages[p]) == crate]
        owners = set()
        for candidate in candidates:
            if candidate in direct:
                owners.add(candidate)
                continue
            reach = {d: dist[candidate] for d, dist in distances.items() if candidate in dist}
            closest = min(reach.values())
            owners.update(d for d, distance in reach.items() if distance == closest)
        for owner in sorted(owners):
            name = graph.packages[owner]["name"]
            if name in private:
                continue
            exposure = exposures.setdefault(name, Exposure(name))
            if crate == library_name(graph.packages[owner]):
                exposure.items[:0] = sorted(items)
            else:
                exposure.items.extend(sorted(items))
    return exposures


def foreign_api_items(doc: dict) -> dict[str, set[str]]:
    """Return the foreign items the public API refers to, grouped by crate name."""
    crates = {int(id): crate["name"] for id, crate in doc["external_crates"].items()}
    items = {}
    for crate_id, references in foreign_references(doc).items():
        crate = crates.get(crate_id)
        if crate is None or crate in STD_CRATES:
            continue
        items.setdefault(crate, set()).update(path for path, _ in references)
    return items


def foreign_references(doc: dict) -> dict[int, set[tuple[str, str]]]:
    """Return the foreign items the public API refers to as (path, kind), by crate id."""
    paths = doc["paths"]
    index = doc["index"]
    references = {}
    for item in index.values():
        if item["crate_id"] != 0:
            continue
        inner = item["inner"]
        if "impl" in inner and (inner["impl"].get("blanket_impl") or inner["impl"].get("is_synthetic")):
            continue
        for referenced in referenced_ids(inner):
            summary = paths.get(str(referenced))
            if summary is not None:
                crate_id = summary["crate_id"]
                identity = ("::".join(summary["path"]), summary["kind"])
            elif str(referenced) in index:
                foreign = index[str(referenced)]
                crate_id = foreign["crate_id"]
                identity = (foreign.get("name") or "", next(iter(foreign["inner"]), ""))
            else:
                continue
            if crate_id != 0:
                references.setdefault(crate_id, set()).add(identity)
    return references


def foreign_reference_sites(doc: dict) -> dict[int, set[tuple[str, str, str, str]]]:
    """Return the foreign references of the public API as (site, position, path, kind), by crate id.

    The site names the local item holding the reference and the position is the JSON path of the
    reference within that item, so both are stable across snapshots of the same API.
    """
    paths = doc["paths"]
    index = doc["index"]
    sites = local_sites(doc)
    holders = {}
    references = {}
    for id, item in index.items():
        if item["crate_id"] != 0:
            continue
        inner = item["inner"]
        if "impl" in inner and (inner["impl"].get("blanket_impl") or inner["impl"].get("is_synthetic")):
            continue
        for position, referenced in referenced_positions(inner):
            summary = paths.get(str(referenced))
            if summary is not None:
                crate_id = summary["crate_id"]
                path, kind = "::".join(summary["path"]), summary["kind"]
            elif str(referenced) in index:
                foreign = index[str(referenced)]
                crate_id = foreign["crate_id"]
                path, kind = foreign.get("name") or "", next(iter(foreign["inner"]), "")
            else:
                continue
            if crate_id != 0:
                key = (sites[id], position, path, kind)
                if holders.setdefault(key, crate_id) != crate_id:
                    raise Undecided(
                        f"items of the public API named `{sites[id]}` refer to `{path}` "
                        "from different crates"
                    )
                references.setdefault(crate_id, set()).add(key)
    return references


def local_sites(doc: dict) -> dict[str, str]:
    """Return a name for every local item that is stable across snapshots of the same API."""
    index = doc["index"]
    paths = doc["paths"]
    parents = {}
    for id, item in index.items():
        if item["crate_id"] == 0:
            for child in child_ids(item["inner"]):
                parents.setdefault(str(child), id)
    sites = {}

    def site(id: str) -> str:
        if id in sites:
            return sites[id]
        item = index[id]
        inner = item["inner"]
        summary = paths.get(id)
        if "impl" in inner:
            impl = inner["impl"]
            implemented = type_text(impl.get("for"), paths)
            trait = impl.get("trait")
            name = f"impl {type_text(trait, paths)} for {implemented}" if trait else f"impl {implemented}"
        elif summary is not None and summary["crate_id"] == 0:
            name = "::".join(summary["path"])
        else:
            parent = parents.get(id)
            prefix = site(parent) if parent is not None else "?"
            name = f"{prefix}::{item.get('name') or next(iter(inner), '')}"
        sites[id] = name
        return name

    for id, item in index.items():
        if item["crate_id"] == 0:
            site(id)
    return sites


def child_ids(inner: dict) -> list[int]:
    """Return the ids of the items an item contains: module and impl items, fields, variants."""
    children = []
    stack = [inner]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in ("items", "fields", "variants", "tuple") and isinstance(value, list):
                    children.extend(v for v in value if isinstance(v, int))
                if key != "impls" and isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(v for v in current if isinstance(v, (dict, list)))
    return children


def type_text(value, paths: dict) -> str:
    """Return a type or path from rustdoc JSON as Rust-like text with canonical item paths.

    Paths are resolved through their item ids, so equally named types of different modules or
    crates differ; anything unknown falls back to JSON.
    """
    if value is None:
        return "_"
    if isinstance(value, dict) and "path" in value and "args" in value:
        summary = paths.get(str(value.get("id")))
        name = "::".join(summary["path"]) if summary is not None else f"?{value['path']}"
        return name + args_text(value["args"], paths)
    if isinstance(value, dict) and len(value) == 1:
        kind, inner = next(iter(value.items()))
        if kind == "resolved_path":
            return type_text(inner, paths)
        if kind in ("generic", "primitive") and isinstance(inner, str):
            return inner
        if kind == "borrowed_ref":
            return "&" + ("mut " if inner.get("is_mutable") else "") + type_text(inner.get("type"), paths)
        if kind == "tuple":
            return "(" + ", ".join(type_text(t, paths) for t in inner) + ")"
        if kind == "slice":
            return "[" + type_text(inner, paths) + "]"
        if kind == "array":
            return f"[{type_text(inner.get('type'), paths)}; {inner.get('len')}]"
    return json.dumps(without_ids(value), sort_keys=True, separators=(",", ":"))


def args_text(args, paths: dict) -> str:
    if not args:
        return ""
    if "angle_bracketed" in args:
        parts = []
        for arg in args["angle_bracketed"].get("args", []):
            if "type" in arg:
                parts.append(type_text(arg["type"], paths))
            elif "lifetime" in arg:
                parts.append(arg["lifetime"])
            else:
                parts.append(json.dumps(without_ids(arg), sort_keys=True, separators=(",", ":")))
        constraints = args["angle_bracketed"].get("constraints", [])
        if constraints:
            parts.append(json.dumps(without_ids(constraints), sort_keys=True, separators=(",", ":")))
        return "<" + ", ".join(parts) + ">" if parts else ""
    return json.dumps(without_ids(args), sort_keys=True, separators=(",", ":"))


def without_ids(value):
    if isinstance(value, dict):
        return {k: without_ids(v) for k, v in value.items() if k != "id"}
    if isinstance(value, list):
        return [without_ids(v) for v in value]
    return value


def referenced_positions(value) -> set[tuple[str, int]]:
    """Return every item id a JSON value refers to through an `id` field, with its JSON path."""
    found = set()
    stack = [(value, "")]
    while stack:
        current, position = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if key == "id" and isinstance(child, int):
                    found.add((position, child))
                elif isinstance(child, (dict, list)):
                    stack.append((child, f"{position}/{key}"))
        elif isinstance(current, list):
            for i, child in enumerate(current):
                if isinstance(child, (dict, list)):
                    stack.append((child, f"{position}/{i}"))
    return found


def referenced_ids(value) -> set[int]:
    """Return every item id a JSON value refers to through an `id` field."""
    ids = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if key == "id" and isinstance(child, int):
                    ids.add(child)
                elif isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(current, list):
            stack.extend(child for child in current if isinstance(child, (dict, list)))
    return ids


def rustdoc_json(
    directory: str, package: dict, toolchain: str, target: str
) -> tuple[dict, dict[str, str]]:
    """Return the rustdoc JSON of a crate and the build artifacts, from real file path to package id."""
    build = subprocess.run(
        [
            "cargo",
            f"+{toolchain}",
            "rustdoc",
            "--quiet",
            "--message-format",
            "json",
            "--manifest-path",
            os.path.join(directory, "Cargo.toml"),
            "--package",
            f"{package['name']}@{package['version']}",
            "--lib",
            "--all-features",
            "--target-dir",
            target,
            "--",
            "-Zunstable-options",
            "--output-format",
            "json",
        ],
        check=True,
        cwd=directory,
        stdout=subprocess.PIPE,
        text=True,
    )
    artifacts = {}
    for line in build.stdout.splitlines():
        message = json.loads(line)
        if message.get("reason") == "compiler-artifact":
            for filename in message["filenames"]:
                artifacts[os.path.realpath(filename)] = message["package_id"]
    with open(os.path.join(target, "doc", f"{library_name(package)}.json")) as file:
        return json.load(file), artifacts


def rustc_sysroot(toolchain: str) -> str:
    output = subprocess.run(
        ["rustc", f"+{toolchain}", "--print", "sysroot"], check=True, capture_output=True, text=True
    ).stdout
    return os.path.realpath(output.strip())


def is_workspace_dependency(package: dict, dependency: str) -> bool:
    with open(package["manifest_path"], "rb") as file:
        manifest = tomllib.load(file)
    tables = [manifest.get("dependencies", {})]
    tables.extend(
        target.get("dependencies", {}) for target in manifest.get("target", {}).values()
    )
    for table in tables:
        for key, spec in table.items():
            if not isinstance(spec, dict) or not spec.get("workspace"):
                continue
            if key == dependency or spec.get("package") == dependency:
                return True
    return False


def workspace_packages(metadata: dict) -> list[dict]:
    members = set(metadata["workspace_members"])
    return [p for p in metadata["packages"] if p["id"] in members]


def published_libraries(metadata: dict) -> list[dict]:
    return [
        p for p in workspace_packages(metadata) if is_published(p) and library_name(p)
    ]


def is_published(package: dict) -> bool:
    return package.get("publish") is None or len(package["publish"]) > 0


def library_name(package: dict) -> str | None:
    for target in package["targets"]:
        if any(kind in ("lib", "rlib", "dylib", "proc-macro") for kind in target["kind"]):
            return target["name"].replace("-", "_")
    return None


def cargo_metadata(directory: str, no_deps: bool) -> dict:
    command = [
        "cargo",
        "metadata",
        "--format-version",
        "1",
        "--manifest-path",
        os.path.join(directory, "Cargo.toml"),
    ]
    command.append("--no-deps" if no_deps else "--all-features")
    output = subprocess.run(
        command, check=True, capture_output=True, text=True, cwd=directory
    ).stdout
    return json.loads(output)


def target_dir(repo: str, args: argparse.Namespace) -> str:
    return os.path.abspath(
        args.target_dir
        or os.environ.get("CARGO_TARGET_DIR")
        or os.path.join(repo, "target", "public-deps-check")
    )


def write_outputs(result: Result) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as file:
        file.write(f"finding={result.finding}\n")
        file.write(f"head-sha={result.head_sha}\n")


class worktree:
    def __init__(self, repo: str, commit: str):
        self.repo = repo
        self.commit = commit
        self.directory = None

    def __enter__(self) -> str:
        self.directory = tempfile.mkdtemp(prefix="public-deps-check-")
        git(self.repo, "worktree", "add", "--quiet", "--detach", self.directory, self.commit)
        return self.directory

    def __exit__(self, *exc) -> None:
        git(self.repo, "worktree", "remove", "--force", self.directory)
        shutil.rmtree(self.directory, ignore_errors=True)


def git(directory: str, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", directory, *args], capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class GitError(Exception):
    pass


def authorized_env(header: str) -> dict:
    """Return the environment with an HTTP header for git, kept out of command arguments."""
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": header,
    }


class Refused(Exception):
    pass


class GitHub:
    def __init__(self, token: str, repository: str):
        self.token = token
        self.base = f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}/repos/{repository}"

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)


def create_marked_commit(api: GitHub, pr_number: int, head: str) -> tuple[str, str]:
    """Create the marked commit for a Dependabot PR and return the branch and the new commit."""
    pr = api.get(f"/pulls/{pr_number}")
    if pr["user"]["login"] != "dependabot[bot]":
        raise Refused(f"PR #{pr_number} is not from Dependabot")
    if pr["head"]["sha"] != head:
        raise Refused(f"PR #{pr_number} head is {pr['head']['sha']}, not {head}")
    if pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]:
        raise Refused(f"PR #{pr_number} comes from a fork")
    commit = api.get(f"/commits/{head}")
    if len(commit["parents"]) != 1:
        raise Refused(f"{head} is not a single-parent commit")
    if not any(os.path.basename(f["filename"]) == "Cargo.toml" for f in commit.get("files", [])):
        raise Refused(f"{head} changes no Cargo.toml")
    message = marked_message(commit["commit"]["message"])
    new = api.post(
        "/git/commits",
        {
            "message": message,
            "tree": commit["commit"]["tree"]["sha"],
            "parents": [commit["parents"][0]["sha"]],
        },
    )
    return pr["head"]["ref"], new["sha"]


def marked_message(message: str) -> str:
    match = SUBJECT.match(message)
    if match is None or not match.group("prefix").startswith("build(deps)"):
        raise Refused("the commit subject does not start with `build(deps):`")
    if match.group("bang"):
        raise Refused("the commit subject already has a `!`")
    return f"{match.group('prefix')}!:{match.group('rest')}"


def push_with_lease(
    git_dir: str, url: str, branch: str, expected: str, new_sha: str, env: dict | None = None
) -> bool:
    """Move a remote branch to a commit unless it no longer points at the expected commit."""
    push = subprocess.run(
        [
            "git",
            "-C",
            git_dir,
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/{branch}:{expected}",
            url,
            f"{new_sha}:refs/heads/{branch}",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if push.returncode == 0:
        return True
    if "[rejected] (stale info)" in push.stdout or "(stale info)" in push.stderr:
        return False
    raise GitError(f"git push failed: {(push.stdout + push.stderr).strip()}")


if __name__ == "__main__":
    sys.exit(main())
