# Publishing releases

Develop on `nvg-develop` and publish stable major 2 releases from `v2`.
The first release is `2.0.0`; existing version 1 tags and releases stay intact.
Distribution uses Git tags and GitHub Releases. The workflows do not publish to PyPI.

## Release a version

1. Merge the implementation of this circuit, and then the changes to release, into `nvg-develop`.
   Complete the one-time repository setup below before the first run.
2. Open **Actions → Prepare release → Run workflow**, select `nvg-develop`, and run it.
   [`release-pr.yaml`](../.github/workflows/release-pr.yaml) creates or reuses the PR from `nvg-develop` to `v2`.
3. Review the changes and normal CI checks. If GitHub requests **Approve workflows to run**, approve them.
   Approve the PR and merge it with **Create a merge commit**.
4. **Release**, defined in [`release.yaml`](../.github/workflows/release.yaml), runs after that PR is merged.
   Its run summary shows the calculated version before publication. It updates the version and changelog on `v2`,
   pushes the release commit and tag, publishes the GitHub Release, and creates or reuses a PR back to `nvg-develop`.
5. Verify the published release, then review and merge that synchronization PR with **Create a merge commit**.
   Finish this before preparing another release so development includes the release metadata and tag history.

**Prepare release** only opens the promotion PR; version calculation happens in **Release**.
Merging the implementation into `nvg-develop` alone does not publish anything.

## Versions

The publication workflow uses Python Semantic Release `10.7.0` and `build==1.3.0`.
The first major 2 publication uses `--major` to produce `2.0.0`; later releases use normal semantic versioning.
Use Conventional Commits: `fix:` normally produces a patch and `feat:` a minor release.
Breaking changes that require a new major need a separate release branch and policy.
If there are no version-changing commits, a successful run creates no new release tag.

## Pending one-time repository setup

This setup has not been provisioned by the implementation. A maintainer must complete it before publishing:

- Create `v2` from the last published version 1 tag, `v1.2.5`.
- Protect `v2` with reviewed PRs and the normal CI checks. Limit the `DeployKey` bypass to its `v2` ruleset;
  this actor covers repository deploy keys. Keep development protections and bypass permissions separate.
- Create environment `release-v2`, restricted to branch `v2`. Store its private SSH deploy key as
  `RELEASE_DEPLOY_KEY` and register the public key as a repository deploy key with write access.
- Enable **Settings → Actions → General → Workflow permissions → Allow GitHub Actions to create and approve pull requests**.
  PR creation uses `GITHUB_TOKEN`; a maintainer reviews and approves the PRs.

Git pushes use the SSH deploy key; GitHub API operations use `GITHUB_TOKEN`.
The workflows do not configure repository protections, create credentials, or select a GitHub App secret automatically.

## Retry and recovery

If a run fails, inspect its log and the remote release, tag, and branch state before retrying.
After publication, a failed synchronization-PR step can be retried to create or reuse that PR.
Partial publication failures may need manual repair; do not force or replace published tags.
Resolve synchronization-PR conflicts and merge it back before the next release.

## Use the release

After `v2.0.0` is published, set `rev: v2.0.0` in the `test_utils` entry of `.pre-commit-config.yaml`.
For Python: `pip install "git+https://github.com/NovigoTechnology/test_utils.git@v2.0.0"`.
Use a published version tag for consumers; `v2` is the publication branch.
