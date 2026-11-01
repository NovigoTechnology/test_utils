# Releases

The release circuit is:

`fix branch → PR → nvg-develop → PR → v2 → version, tag and GitHub Release`

## Publish

1. Merge reviewed changes into `nvg-develop` using Conventional Commit messages.
2. Open **Actions → Prepare release PR → Run workflow**, select `nvg-develop` and run it. Review the generated PR from `nvg-develop` to `v2`.
3. Merge that promotion using **Create a merge commit**. This preserves the individual commits used to calculate the version.
4. Open its **Release** workflow run. It calculates the version, builds the package, updates `pyproject.toml` and `CHANGELOG.md` on `v2`, pushes the release commit and tag, and publishes the GitHub Release.

No manual version edit is required. Merging into `nvg-develop`, closing an unmerged PR, or pushing a release commit does not trigger publication.

**Prepare release PR** runs daily at **21:00 Argentina (00:00 UTC)** and can also be started manually. Scheduled runs use the default branch, `nvg-develop`. It links an existing open promotion PR instead of creating a duplicate, and skips creation when there are no new development commits to promote. It does not merge PRs or publish releases.

## Versions

The first release from `v2` is **2.0.0**. Historical tags are preserved.
Later versions follow Conventional Commits:

| Commit | Version change |
| --- | --- |
| `fix:` or `perf:` | Patch: `2.0.0 → 2.0.1` |
| `feat:` | Minor: `2.0.1 → 2.1.0` |
| `!` or `BREAKING CHANGE:` | Major; publication stops if the result leaves the destination branch's major |
| `docs:`, `test:`, `chore:`, `ci:`, `style:`, `refactor:`, `build:` | No release unless they declare a breaking change |

The highest required change wins. Without version-changing commits, no new tag or release is created.

## Prepare the next major release

For example, to move from `v2` to `v3`:

1. Create `v3` from the latest published `v2` tag, preserving the release history and existing tags.
2. Prepare a reviewed PR into `nvg-develop` with the new major's changes and configuration:
   - In `.github/workflows/release.yaml`, change `pull_request.branches` to `[v3]` and `env.BASE_BRANCH` to `v3`.
   - In `.github/workflows/prepare-release.yaml`, change `env.BASE_BRANCH` to `v3`.
   - In `pyproject.toml`, change `[tool.semantic_release.branches.stable].match` from `"^v2$"` to `"^v3$"`. This is a manual preparation step; the workflow does not edit that configuration.
3. Check that branch protections cover `v3` and the publication token is permitted to push its release commit and tag.
4. After merging the preparation PR into `nvg-develop`, run **Prepare release PR**, review the `nvg-develop → v3` promotion and merge it with **Create a merge commit**.
5. Verify the Release run, tag and GitHub Release for `v3.0.0`.

The publication step derives the major from `BASE_BRANCH`: `v3` selects tags `v3.*`, permits versions `3.x`, and requires `3.0.0` for its first publication. No manual version edit is needed. A later breaking change that requires `4.x` stops publication from `v3` until the next major is prepared.

## Requirements and failures

`v2` starts at the last published release, `v1.2.5`.
The workflow uses the existing organization secret `NOVIGO_BOT_20250619` for Git pushes and GitHub Releases.
Its token must be allowed to push release commits to protected `v2` and create tags and releases. No SSH key is used.

The workflow pins Python Semantic Release to `10.7.0`; it builds with `build==1.3.0` before pushing the release.
If publication fails, inspect the run, branch, tag and GitHub Release before retrying. Do not overwrite published tags.
A rerun stops when `v2` has advanced beyond the promotion commit, including after a release commit was pushed.

Consumers should pin published tags such as `v2.0.0`, rather than the mutable `v2` branch.
