import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import public_deps_check as check  # noqa: E402


def nightly() -> str:
    toolchain = os.environ.get("PUBLIC_DEPS_TOOLCHAIN")
    if toolchain:
        return toolchain
    version = subprocess.run(["rustc", "--version"], capture_output=True, text=True).stdout
    return "nightly-" + re.search(r"\d{4}-\d{2}-\d{2}", version).group(0)


TOOLCHAIN = nightly()
TARGET = tempfile.mkdtemp(prefix="public-deps-check-target-")

DEPS = {
    "dep_reexport": "pub struct Reexported;",
    "dep_sig": "pub struct S;",
    "dep_trait": "pub trait Tr {}",
    "dep_private": "pub struct P;",
    "dep_dev": "pub fn helper() {}",
    "dep_c": "pub struct Thing;",
    "dep_b": "pub use dep_c::Thing;\npub fn make() -> Thing { Thing }",
    "dep_marker": "pub trait Marker {}\npub use dep_derive::Marker;",
}

DERIVE = """
use proc_macro::{TokenStream, TokenTree};

#[proc_macro_derive(Marker)]
pub fn derive(input: TokenStream) -> TokenStream {
    let mut tokens = input.into_iter();
    while let Some(token) = tokens.next() {
        if let TokenTree::Ident(ident) = &token {
            if ident.to_string() == "struct" {
                if let Some(TokenTree::Ident(name)) = tokens.next() {
                    return format!("impl ::dep_marker::Marker for {name} {{}}").parse().unwrap();
                }
            }
        }
    }
    panic!("no struct")
}
"""

SUBJECT = """
pub use dep_reexport::Reexported;

#[derive(dep_marker::Marker)]
pub struct Derived;

pub struct Local;

impl dep_trait::Tr for Local {}

pub fn signature() -> dep_sig::S {
    private();
    dep_sig::S
}

fn private() -> dep_private::P {
    dep_private::P
}

#[cfg(feature = "transitive")]
pub fn transitive() -> dep_b::Thing {
    dep_b::make()
}

#[cfg(test)]
mod tests {
    #[test]
    fn helper() {
        dep_dev::helper();
    }
}
"""


def dependency_lines(versions: dict[str, str], workspace: bool) -> str:
    lines = []
    for name in ["dep_reexport", "dep_sig", "dep_trait", "dep_private", "dep_marker"]:
        if workspace and name == "dep_sig":
            lines.append(f"{name} = {{ workspace = true }}")
        else:
            path = "../deps" if workspace else "deps"
            lines.append(f'{name} = {{ path = "{path}/{name}", version = "{versions[name]}" }}')
    path = "../deps" if workspace else "deps"
    lines.append(
        f'dep_b = {{ path = "{path}/dep_b", version = "{versions["dep_b"]}", optional = true }}'
    )
    return "\n".join(lines)


class Fixture:
    """A git repository with a published crate `subject` and path dependencies under `deps/`."""

    def __init__(
        self, workspace: bool = False, private_dep_c: bool = False, cargo_env: bool = False
    ):
        self.workspace = workspace
        self.private_dep_c = private_dep_c
        self.cargo_env = cargo_env
        self.dep_b_uses_dep_c_2 = False
        self.dir = tempfile.mkdtemp(prefix="public-deps-check-fixture-")
        self.versions = {name: "1.0.0" for name in [*DEPS, "dep_derive"]}
        self.description = "subject"
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")
        self.write_all()
        self.commit("feat: initial")

    def git(self, *args: str) -> str:
        return check.git(self.dir, *args)

    def write(self, path: str, content: str) -> None:
        path = os.path.join(self.dir, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as file:
            file.write(textwrap.dedent(content).lstrip())

    def write_all(self) -> None:
        for name, source in DEPS.items():
            deps = ""
            if name == "dep_b":
                dep_c = "../dep_c2" if self.dep_b_uses_dep_c_2 else "../dep_c"
                deps = f'[dependencies]\ndep_c = {{ path = "{dep_c}" }}\n'
            if name == "dep_marker":
                deps = '[dependencies]\ndep_derive = { path = "../dep_derive" }\n'
            self.write(
                f"deps/{name}/Cargo.toml",
                f'[package]\nname = "{name}"\nversion = "{self.versions[name]}"\n'
                f'edition = "2024"\n\n{deps}',
            )
            self.write(f"deps/{name}/src/lib.rs", source)
        self.write(
            "deps/dep_derive/Cargo.toml",
            f'[package]\nname = "dep_derive"\nversion = "{self.versions["dep_derive"]}"\n'
            'edition = "2024"\n\n[lib]\nproc-macro = true\n',
        )
        self.write("deps/dep_derive/src/lib.rs", DERIVE)
        self.write(
            "deps/dep_c2/Cargo.toml",
            '[package]\nname = "dep_c"\nversion = "2.0.0"\nedition = "2024"\n',
        )
        self.write("deps/dep_c2/src/lib.rs", DEPS["dep_c"])
        source = SUBJECT
        extra = ""
        if self.private_dep_c:
            source += "\n#[allow(dead_code)]\nfn private_c() -> dep_c::Thing {\n    dep_c::Thing\n}\n"
            path = "../deps" if self.workspace else "deps"
            extra += f'dep_c = {{ path = "{path}/dep_c", version = "1.0.0" }}\n'
        if self.cargo_env:
            source += '\npub const VALUE: &str = env!("PUBLIC_DEPS_FIXTURE");\n'
            self.write(".cargo/config.toml", '[env]\nPUBLIC_DEPS_FIXTURE = "set"\n')

        package = (
            f'[package]\nname = "subject"\nversion = "0.1.0"\nedition = "2024"\n'
            f'description = "{self.description}"\n\n'
            f"[dependencies]\n{dependency_lines(self.versions, self.workspace)}\n{extra}\n"
            f'[dev-dependencies]\ndep_dev = {{ path = "{"../" if self.workspace else ""}deps/dep_dev", '
            f'version = "{self.versions["dep_dev"]}" }}\n\n'
            '[features]\ntransitive = ["dep:dep_b"]\n'
        )
        if self.workspace:
            self.write(
                "Cargo.toml",
                '[workspace]\nmembers = ["subject"]\nresolver = "3"\n\n'
                "[workspace.dependencies]\n"
                f'dep_sig = {{ path = "deps/dep_sig", version = "{self.versions["dep_sig"]}" }}\n',
            )
            self.write("subject/Cargo.toml", package)
            self.write("subject/src/lib.rs", source)
        else:
            self.write("Cargo.toml", package + '\n[workspace]\nexclude = ["deps"]\n')
            self.write("src/lib.rs", source)

    def commit(self, message: str) -> str:
        subprocess.run(
            ["cargo", "generate-lockfile", "--offline", "--quiet"], cwd=self.dir, check=True
        )
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", message)
        return self.git("rev-parse", "HEAD")

    def bump(self, name: str, version: str, message: str) -> str:
        self.versions[name] = version
        self.write_all()
        return self.commit(message)

    def analyze(self, base: str, head: str) -> check.Result:
        return check.analyze(self.dir, base, head, TOOLCHAIN, TARGET)


class CompatibilityTest(unittest.TestCase):
    def test_caret_requirements(self):
        self.assertEqual(check.compatibility_key("^6.0"), (6,))
        self.assertEqual(check.compatibility_key("^5.5"), (5,))
        self.assertEqual(check.compatibility_key("^0.50"), (0, 50))
        self.assertEqual(check.compatibility_key("^0.51.0"), (0, 51))
        self.assertEqual(check.compatibility_key("^0.0.3"), (0, 0, 3))
        self.assertEqual(check.compatibility_key("1"), (1,))

    def test_other_requirements_are_undecided(self):
        for requirement in ["~1.2", "=1.2.3", ">=1, <3", "*"]:
            with self.assertRaises(check.Undecided):
                check.compatibility_key(requirement)


class MarkerTest(unittest.TestCase):
    def test_has_marker(self):
        self.assertTrue(check.has_marker("build(deps)!: bump utoipa"))
        self.assertTrue(check.has_marker("feat!: drop x"))
        self.assertTrue(check.has_marker("build(deps): bump\n\nBREAKING CHANGE: utoipa 6"))
        self.assertFalse(check.has_marker("build(deps): bump utoipa from 5.5.0 to 6.0.0"))
        self.assertFalse(check.has_marker("Merge abc into def"))

    def test_marked_message(self):
        self.assertEqual(
            check.marked_message("build(deps): bump utoipa\n\nBody text\n"),
            "build(deps)!: bump utoipa\n\nBody text\n",
        )
        with self.assertRaises(check.Refused):
            check.marked_message("build(deps)!: bump utoipa")
        with self.assertRaises(check.Refused):
            check.marked_message("ci(deps): bump action")


class ExposureTest(unittest.TestCase):
    def test_exposed_dependencies(self):
        fixture = Fixture()
        metadata = check.cargo_metadata(fixture.dir, no_deps=False)
        package = check.published_libraries(metadata)[0]
        exposures = check.exposed_dependencies(
            fixture.dir, package, check.DependencyGraph(metadata), TOOLCHAIN, TARGET
        )
        self.assertEqual(
            sorted(exposures),
            ["dep_b", "dep_marker", "dep_reexport", "dep_sig", "dep_trait"],
        )
        self.assertIn("dep_marker::Marker", exposures["dep_marker"].items)
        self.assertIn("dep_c::Thing", exposures["dep_b"].items)

    def test_private_exception(self):
        fixture = Fixture()
        metadata = check.cargo_metadata(fixture.dir, no_deps=False)
        package = check.published_libraries(metadata)[0]
        package["metadata"] = {"public-deps": {"private": ["dep_trait"]}}
        exposures = check.exposed_dependencies(
            fixture.dir, package, check.DependencyGraph(metadata), TOOLCHAIN, TARGET
        )
        self.assertNotIn("dep_trait", exposures)


class AnalyzeTest(unittest.TestCase):
    def test_exposed_bump_without_marker(self):
        fixture = Fixture()
        base = fixture.git("rev-parse", "HEAD")
        head = fixture.bump("dep_sig", "2.0.0", "build(deps): bump dep_sig")
        result = fixture.analyze(base, head)
        self.assertEqual(result.finding, check.FINDING_MISSING_MARKER, result.messages)
        self.assertEqual(result.head_sha, head)

    def test_exposed_bump_with_marker(self):
        fixture = Fixture()
        base = fixture.git("rev-parse", "HEAD")
        head = fixture.bump("dep_sig", "2.0.0", "build(deps)!: bump dep_sig")
        self.assertEqual(fixture.analyze(base, head).finding, check.FINDING_NONE)

    def test_derive_and_optional_transitive_bumps(self):
        for name in ["dep_marker", "dep_b"]:
            fixture = Fixture()
            base = fixture.git("rev-parse", "HEAD")
            head = fixture.bump(name, "2.0.0", f"build(deps): bump {name}")
            result = fixture.analyze(base, head)
            self.assertEqual(result.finding, check.FINDING_MISSING_MARKER, (name, result.messages))

    def test_bumps_that_are_not_breaking(self):
        cases = [("dep_private", "2.0.0"), ("dep_dev", "2.0.0"), ("dep_sig", "1.1.0")]
        for name, version in cases:
            fixture = Fixture()
            base = fixture.git("rev-parse", "HEAD")
            head = fixture.bump(name, version, f"build(deps): bump {name}")
            result = fixture.analyze(base, head)
            self.assertEqual(result.finding, check.FINDING_NONE, (name, result.messages))

    def test_no_cargo_toml_change(self):
        fixture = Fixture()
        base = fixture.git("rev-parse", "HEAD")
        fixture.write("README.md", "readme")
        head = fixture.commit("docs: add readme")
        self.assertEqual(fixture.analyze(base, head).finding, check.FINDING_NONE)

    def test_compares_with_merge_base(self):
        fixture = Fixture()
        fork = fixture.git("rev-parse", "HEAD")
        main = fixture.bump("dep_sig", "2.0.0", "build(deps)!: bump dep_sig")
        fixture.git("checkout", "--quiet", "-b", "branch", fork)
        fixture.versions["dep_sig"] = "1.0.0"
        fixture.description = "changed"
        fixture.write_all()
        head = fixture.commit("chore: change description")
        result = fixture.analyze(main, head)
        self.assertEqual(result.finding, check.FINDING_NONE, result.messages)

    def test_bump_that_changes_a_crate_also_used_in_another_version(self):
        fixture = Fixture(private_dep_c=True)
        base = fixture.git("rev-parse", "HEAD")
        fixture.dep_b_uses_dep_c_2 = True
        head = fixture.bump("dep_b", "2.0.0", "build(deps): bump dep_b")
        result = fixture.analyze(base, head)
        self.assertEqual(result.finding, check.FINDING_MISSING_MARKER, result.messages)

    def test_cargo_config_of_the_analyzed_commit(self):
        fixture = Fixture(cargo_env=True)
        base = fixture.git("rev-parse", "HEAD")
        head = fixture.bump("dep_sig", "2.0.0", "build(deps): bump dep_sig")
        result = fixture.analyze(base, head)
        self.assertEqual(result.finding, check.FINDING_MISSING_MARKER, result.messages)

    def test_workspace_dependency(self):
        fixture = Fixture(workspace=True)
        base = fixture.git("rev-parse", "HEAD")
        head = fixture.bump("dep_sig", "2.0.0", "build(deps)!: bump dep_sig")
        result = fixture.analyze(base, head)
        self.assertEqual(result.finding, check.FINDING_WORKSPACE_TRUE, result.messages)


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.remote = tempfile.mkdtemp(prefix="public-deps-check-remote-")
        check.git(self.remote, "init", "--quiet", "--bare")
        self.work = tempfile.mkdtemp(prefix="public-deps-check-work-")
        check.git(self.work, "init", "--quiet", "--initial-branch", "branch")
        check.git(self.work, "config", "user.email", "test@example.com")
        check.git(self.work, "config", "user.name", "test")
        check.git(self.work, "config", "commit.gpgsign", "false")
        parent = self.commit("feat: initial")
        self.analyzed = self.commit("build(deps): bump x")
        check.git(self.work, "push", "--quiet", self.remote, "branch")
        self.marked = check.git(
            self.work, "commit-tree", "HEAD^{tree}", "-p", parent, "-m", "build(deps)!: bump x"
        )

    def commit(self, message: str) -> str:
        check.git(self.work, "commit", "--quiet", "--allow-empty", "--message", message)
        return check.git(self.work, "rev-parse", "HEAD")

    def remote_branch(self) -> str:
        return check.git(self.remote, "rev-parse", "refs/heads/branch")

    def test_push_when_branch_unchanged(self):
        pushed = check.push_with_lease(
            self.work, self.remote, "branch", self.analyzed, self.marked
        )
        self.assertTrue(pushed)
        self.assertEqual(self.remote_branch(), self.marked)

    def test_push_rejected_when_branch_moved(self):
        newer = self.commit("build(deps): bump x again")
        check.git(self.work, "push", "--quiet", self.remote, "branch")
        pushed = check.push_with_lease(
            self.work, self.remote, "branch", self.analyzed, self.marked
        )
        self.assertFalse(pushed)
        self.assertEqual(self.remote_branch(), newer)

    def test_push_fails_when_remote_refuses(self):
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w") as file:
            file.write("#!/bin/sh\necho policy >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        with self.assertRaises(check.GitError):
            check.push_with_lease(self.work, self.remote, "branch", self.analyzed, self.marked)
        self.assertEqual(self.remote_branch(), self.analyzed)


class CredentialTest(unittest.TestCase):
    def test_git_error_does_not_contain_credentials(self):
        directory = tempfile.mkdtemp(prefix="public-deps-check-credential-")
        check.git(directory, "init", "--quiet", "--bare")
        env = check.authorized_env("Authorization: Basic c2VjcmV0LXRva2Vu")
        missing = os.path.join(directory, "missing.git")
        with self.assertRaises(check.GitError) as raised:
            check.git(directory, "fetch", "--quiet", missing, "main", env=env)
        self.assertNotIn("c2VjcmV0LXRva2Vu", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
