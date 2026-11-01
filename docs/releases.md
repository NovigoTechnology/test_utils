# Publishing releases

Develop on `nvg-develop`; publish stable major 2 releases from `v2`.
The first release is `2.0.0`. Existing version 1 tags and releases stay intact.
Distribution uses Git tags and GitHub Releases; this workflow does not publish to PyPI.

```text
nvg-develop → release PR → v2 → version/tag/GitHub Release → synchronization PR → nvg-develop
```

## Activate the circuit

Merge the implementation PR into `nvg-develop`, complete the repository setup below, and ensure `v2` exists.
That implementation merge does not publish a release. Start **Prepare release** from `nvg-develop`
to open the first promotion PR; merging that PR into `v2` publishes `2.0.0`.

## Publish a release

1. Merge the changes to release into `nvg-develop` using Conventional Commits.
   `fix:` normally produces a patch, `feat:` a minor version, and breaking changes a major version.
2. Open **Actions → Prepare release → Run workflow**, select `nvg-develop`, and run it.
   [`release-pr.yaml`](../.github/workflows/release-pr.yaml) opens or reuses the PR from `nvg-develop` to `v2`
   when the version calculation finds publishable changes.
3. If GitHub shows **Approve workflows to run** on the bot-created PR, approve them.
   Review the tests and the **Release policy** check, including its version preview.
4. Review and approve the release PR, then select **Create a merge commit**.
   Keep this merge method for both release and synchronization PRs so branch history is preserved.
5. [`release.yaml`](../.github/workflows/release.yaml), named **Release**, runs when the release PR is merged.
   It updates the version and changelog on `v2`, pushes the release commit and tag, and publishes the GitHub Release.
6. Verify the published tag and release, then review and merge the synchronization PR from `v2` to `nvg-develop`.
   Select **Create a merge commit** again. Finish this step before preparing the next release.

When there is no version-changing commit, **Prepare release** succeeds with `changed=false` and creates no PR or tag.
The version preview is a calculation, not a publication.

## Version selection

The workflow uses Python Semantic Release `10.7.0` and `build==1.3.0`.
Published, stable major 2 GitHub Releases provide the baseline after the first publication.
Before that, both branches must include the latest published stable major 1 release.
For the first major 2 release only, the version calculation uses `--major` to produce `2.0.0`.
Later releases use normal semantic version calculation. A new major needs its own release branch and policy.

An existing major 2 tag without a matching published release requires investigation;
it is not a reason to overwrite the tag or silently bootstrap another first release.

## Repository setup

A maintainer configures these once:

- Under **Settings → Actions → General → Workflow permissions**, enable
  **Allow GitHub Actions to create and approve pull requests**.
  PR creation uses `GITHUB_TOKEN`; a maintainer still reviews and approves the PR.
- Create the `release-v2` environment and restrict its deployment branch to `v2`.
- Add the private SSH deploy key as environment secret `RELEASE_DEPLOY_KEY`, and register its public key
  as a deploy key with write access to this repository.
- Keep the required PR checks and merge-commit option enabled.

The publication job obtains the deploy key through `release-v2` and uses SSH for Git pushes.
`GITHUB_TOKEN` is used for GitHub API operations, including PRs and releases.
No GitHub App secret is selected automatically. If GitHub requires workflow approval for a bot-created PR,
use **Approve workflows to run** before waiting for checks.

## If a run fails

| Situation | Action |
| --- | --- |
| The release PR exists but checks have not started | Open the PR and approve its workflows when GitHub requests it. |
| The version check says development lacks the published baseline | Merge the previous synchronization PR into `nvg-develop`, then rerun the check. |
| Publication succeeded but creating the synchronization PR failed | Rerun the **Release** job. If `v2` still points to a published release, it creates/reuses the PR without publishing again. If the log says `v2` advanced beyond the promotion, use the latest promotion run. |
| A tag exists without a published GitHub Release, or Git/API publication failed partway through | Inspect the remote tag, release commit, and GitHub Release before retrying; resolve the failed step without forcing or replacing tags. |
| The synchronization PR conflicts | Resolve its conflicts, run the checks, and merge it with a merge commit before the next release. |

The retry path after a completed publication does not make every partial Git/API failure automatically recoverable.
Check the workflow log and remote state for those failures.

## Use a published release

`v2` is the release branch. Pin consumers to a published version tag, for example after `v2.0.0` is available:

```yaml
- repo: https://github.com/NovigoTechnology/test_utils
  rev: v2.0.0
  hooks:
    - id: validate_override_annotations
```

For Python installation, use the same published tag:

```bash
pip install "git+https://github.com/NovigoTechnology/test_utils.git@v2.0.0"
```
