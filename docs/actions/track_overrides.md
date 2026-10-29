# Track Overrides

Replace `<tag-or-commit>` with a published Novigo tag or full commit SHA containing the tools you need.

`track-overrides` is a GitHub Action designed to track method overrides in Python projects.
It automatically detects the base branch from the pull request that triggers the action and compares the code changes in the pull request against the corresponding branch in the repository specified in the method annotations.

Tracking compares the annotated upstream implementation at `HASH` with the selected upstream
revision. Different hashes alone do not imply drift: the tracked implementation must differ.
An invalid or unavailable baseline is reported as unverified, never as unchanged.


## Method Annotation

To track the overrides, the methods must have a comment with the following structure:

```
"""
APP: <app_name>
HASH: <commit_hash>
REPO: <repository_url>
PATH: <file_path>
METHOD: <method_name>
"""
```

- **APP**: The app that contains this override (e.g. `frappe`, `inventory_tools`). Required for pre-commit to correctly scope checks when using `--app` or `--directory`.
- **HASH**: Commit hash of the upstream version when this override was last validated.
- **REPO**: Upstream repository URL.
- **PATH**: Path to the file in the upstream repo.
- **METHOD**: Function name or qualified class method (`Ledger.validate`). Use qualified names when multiple classes define a method with the same name.

Source extraction uses AST and supports async methods and return annotations.
The concrete implementation of an overloaded function is selected; declaration-only or
ambiguous symbols are reported for review. Annotation fields can be reordered or separated
by prose. HTTPS, SSH and SCP GitHub URLs, including .git suffixes, are normalized before
calling the API. Legacy complete annotations in comment blocks remain supported by tracking. Removed or
unresolvable methods are reported for review.

Use [`validate_override_annotations`](../validate_override_annotations.md) alongside tracking
to discover new Frappe overrides whose annotations are missing or invalid.

### Example
```python
def is_system_manager_disabled(user):
	"""
	APP: frappe
	HASH: 171e1d0159cda3b8d9415527590c9c3ca0c827be
	REPO: https://github.com/frappe/frappe/
	PATH: frappe/core/doctype/user/user.py
	METHOD: is_system_manager_disabled
	"""
	# Method implementation here
	pass
```

## Pre-commit Usage

From an app repository in a Bench, no arguments are needed:

```yaml
- id: track_overrides
```

The CLI scans the current Git repository (also when invoked from a subdirectory) and compares
against the committed `HEAD` of each annotated upstream repository. It discovers sibling app
checkouts, or app checkouts under `apps/`, by matching `REPO` against their configured Git
remotes. Remote names such as `origin` and `upstream`, and detached HEADs, are supported.
Uncommitted dependency edits are not part of this comparison.

Historical baseline source is read locally when available. For shallow histories it falls
back to GitHub; private repositories require `GH_TOKEN` or `GITHUB_TOKEN`. Missing or ambiguous
checkouts produce exit 2 with guidance to select `--apps-root` or a remote `--base-branch`.

```bash
track_overrides
track_overrides --apps-root ../
```

Exit codes are 0 for no detected drift, 1 for changes/removal/unresolved tracked methods, and
2 for unverified upstream source or invalid configuration. A scan without annotations exits 0.
The CLI prints the scan directory and comparison target rather than silently skipping work.

Use `--base-branch` to explicitly compare with a remote branch/ref instead of local HEAD.
For **monorepos** (bench structure with apps/frappe, apps/erpnext, etc.), use `--directory .` to scan
the entire workspace—matching GHA behavior. Optionally add `--app` to limit which apps are checked:

```yaml
# Check all apps
- id: track_overrides
  args: ['--directory', '.', '--base-branch', 'version-15']

# Or only frappe and erpnext (requires APP in annotations)
- id: track_overrides
  args: ['--directory', '.', '--app', 'frappe', '--app', 'erpnext', '--base-branch', 'version-15']
```

For **single-app repos**, use `--app`:

```yaml
- id: track_overrides
  args: ['--app', 'inventory_tools', '--base-branch', 'version-15']
```

## GitHub Action Usage

To use the `track-overrides` GitHub Action in your workflow, follow these steps:

### 1. Create a Workflow File

Create a new GitHub Actions workflow file in your repository, e.g., `.github/workflows/overrides.yml`, and add the following content:

```yaml
name: Track Overrides

on:
  pull_request:

jobs:
  track_overrides:
    runs-on: ubuntu-latest
    name: Track Overrides
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Track Overrides
        uses: NovigoTechnology/test_utils/actions/track_overrides@<tag-or-commit>
        with:
          post-comment: false
```
