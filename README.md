# public-deps-check

A GitHub Action that requires a conventional-commit `!` when a pull request bumps a dependency
that appears in a published crate's public API to a semver-incompatible version, for example
utoipa 5.5 to 6.0 or async-nats 0.50 to 0.51. Neither cargo-semver-checks nor Dependabot flag
such a bump as breaking, so release-plz would otherwise propose a patch release.

## How it works

`analyze` compares the head commit of a pull request with the merge base of base and head:

1. `cargo metadata --no-deps` on both commits gives the requirements of normal dependencies of
   every published package. A change is incompatible when the leftmost nonzero version part
   changes. A changed requirement that is not a plain caret requirement fails as `undecided`.
   Dependencies on other members of the workspace are ignored, because release-plz updates
   their requirements itself.
2. For each package with an incompatible bump, `cargo rustdoc` with JSON output on the given
   nightly runs on both commits. It lists the foreign items in the public API: signatures,
   fields, re-exports, bounds and trait impls, derived ones included. Each item is mapped to
   the exact package version that defines it, through the crate file rustdoc records and the
   build artifacts Cargo reports. A crate that cannot be mapped fails as `undecided`.
3. References are matched across both commits by the local item that holds them, their
   position in that item, and the path and kind of the foreign item. The bump is breaking when
   a reference that is public in both now comes from an incompatible version of its package.
   The finding names the bumped direct dependencies that lead to that package.

The check fails with

- `missing-marker` when such a reference exists and the head commit subject has no `!` (and no
  `BREAKING CHANGE:` footer);
- `workspace-true` when the bumped dependency is declared with `workspace = true`, because
  release-plz only sees commits that touch the crate's own directory.

`exposed` lists, per published crate, the direct dependencies whose crates appear in the public
API, attributed through the resolve graph. A dependency can be excluded from that list in the
crate's `Cargo.toml`:

```toml
[package.metadata.public-deps]
private = ["async-stream"]
```

`mark` adds the `!` to the subject of a Dependabot commit (`build(deps): ...` to
`build(deps)!: ...`). It creates the new commit through the GitHub API, so GitHub signs it, and
moves the branch with `git push --force-with-lease`, so a branch that moved meanwhile is left
alone.

## Usage

```yaml
public-deps:
  if: github.event_name == 'pull_request'
  needs: toolchain
  runs-on: ubuntu-latest
  permissions:
    contents: read
  outputs:
    finding: ${{ steps.check.outputs.finding }}
    head-sha: ${{ steps.check.outputs.head-sha }}
  steps:
    - uses: actions/checkout@<sha>
      with:
        fetch-depth: 0
    - uses: dtolnay/rust-toolchain@<sha>
      with:
        toolchain: ${{ needs.toolchain.outputs.nightly }}
    - id: check
      uses: hseeberger/public-deps-check@<sha>
      with:
        command: analyze
        base-sha: ${{ github.event.pull_request.base.sha }}
        head-sha: ${{ github.event.pull_request.head.sha }}
        toolchain: ${{ needs.toolchain.outputs.nightly }}

public-deps-mark:
  needs: public-deps
  if: >-
    ${{ !cancelled()
    && needs.public-deps.outputs.finding == 'missing-marker'
    && github.event.pull_request.user.login == 'dependabot[bot]' }}
  runs-on: ubuntu-latest
  permissions: {}
  steps:
    - id: app-token
      uses: actions/create-github-app-token@<sha>
      with:
        app-id: ${{ secrets.APP_ID }}
        private-key: ${{ secrets.APP_PRIVATE_KEY }}
    - uses: hseeberger/public-deps-check@<sha>
      with:
        command: mark
        head-sha: ${{ needs.public-deps.outputs.head-sha }}
        pr-number: ${{ github.event.pull_request.number }}
        token: ${{ steps.app-token.outputs.token }}
```

The `public-deps` job builds the pull request's dependencies, whose build scripts run arbitrary
code, so it gets no secrets. `public-deps-mark` never checks out or builds the pull request.
For Dependabot pull requests, `APP_ID` and `APP_PRIVATE_KEY` must be Dependabot secrets.

Locally:

```sh
python3 public_deps_check.py analyze --base origin/main --head HEAD --toolchain nightly-2026-11-09
python3 public_deps_check.py exposed --toolchain nightly-2026-11-09
```

## License

Apache-2.0
