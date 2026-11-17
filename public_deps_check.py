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

        full_metadata = cargo_metadata(new_dir, no_deps=False)
        graph = DependencyGraph(full_metadata)
        packages = {p["name"]: p for p in published_libraries(full_metadata)}
        marked = has_marker(git(repo, "log", "-1", "--format=%B", head))
        exposures_by_package = {}
        missing_marker = []
        workspace_true = []
        for bump in bumps:
            package = packages.get(bump.package)
            if package is None:
                continue
            if bump.package not in exposures_by_package:
                exposures_by_package[bump.package] = exposed_dependencies(
                    new_dir, package, graph, toolchain, target
                )
            exposure = exposures_by_package[bump.package].get(bump.dependency)
            if exposure is None:
                continue
            evidence = (
                f"{bump.package}: {bump.dependency} {bump.old} -> {bump.new} is breaking, "
                f"it appears in the public API ({', '.join(exposure.items[:3])})"
            )
            if is_workspace_dependency(package, bump.dependency):
                workspace_true.append(
                    f"{evidence}; declare it in {bump.package}'s own Cargo.toml, "
                    "not with workspace = true"
                )
            elif not marked:
                missing_marker.append(f"{evidence}; the commit subject needs a `!`")

    if workspace_true:
        return Result(FINDING_WORKSPACE_TRUE, head, workspace_true + missing_marker)
    if missing_marker:
        return Result(FINDING_MISSING_MARKER, head, missing_marker)
    return Result(FINDING_NONE, head, ["breaking bumps are marked" if marked else "no public bump"])


def incompatible_bumps(old_metadata: dict, new_metadata: dict) -> list[Bump]:
    old_packages = {p["name"]: p for p in workspace_packages(old_metadata)}
    bumps = []
    for package in workspace_packages(new_metadata):
        if not is_published(package) or package["name"] not in old_packages:
            continue
        old_reqs = normal_requirements(old_packages[package["name"]])
        new_reqs = normal_requirements(package)
        for dependency, new_req in new_reqs.items():
            old_req = old_reqs.get(dependency)
            if old_req is None or old_req == new_req:
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
    crate_items = foreign_api_items(rustdoc_json(directory, package, toolchain, target))
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
    paths = doc["paths"]
    index = doc["index"]
    items = {}
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
                name = "::".join(summary["path"])
            elif str(referenced) in index:
                crate_id = index[str(referenced)]["crate_id"]
                name = index[str(referenced)].get("name") or str(referenced)
            else:
                continue
            if crate_id == 0:
                continue
            crate = crates.get(crate_id)
            if crate is None or crate in STD_CRATES:
                continue
            items.setdefault(crate, set()).add(name)
    return items


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


def rustdoc_json(directory: str, package: dict, toolchain: str, target: str) -> dict:
    subprocess.run(
        [
            "cargo",
            f"+{toolchain}",
            "rustdoc",
            "--quiet",
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
    )
    with open(os.path.join(target, "doc", f"{library_name(package)}.json")) as file:
        return json.load(file)


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
