# Validate override annotations

`validate_override_annotations` checks that your Frappe overrides have complete upstream
tracking annotations. It reads local app checkouts without importing the apps, starting a
site, or downloading dependencies.

Use [`track_overrides`](actions/track_overrides.md) to check whether the annotated upstream
implementation has changed. The two tools serve different steps of the same workflow.

## What gets checked

The validator finds overrides registered in your app's `hooks.py`:

| Hook | Where to add annotations |
| --- | --- |
| `override_whitelisted_methods` | The replacement function. |
| `override_doctype_class` | Each method that overrides an upstream method, including methods inherited from your local mixins. |
| `extend_doctype_class` | Each mixin method that overrides an upstream controller method. |

Put annotations in each overriding function or method's own docstring. Class docstrings
and comments do not cover their methods. New helper methods do not need annotations.

## Run locally

For manual runs, install test_utils in your active Python environment using the
[installation instructions](../README.md#installation). The
[pre-commit setup](#add-it-to-pre-commit) handles installation for the hooks separately.

Have your custom app and its dependencies checked out together, for example:

```text
apps/
  frappe/
  erpnext/
  nvg_base/
  nagus/
    nagus/hooks.py
```

From the Bench root, enter the custom app repository and run:

```bash
cd apps/nagus
validate_override_annotations
```

You can also run directly from the Bench root:

```bash
validate_override_annotations --app apps/nagus --apps-root apps
```

| Option | When to use it |
| --- | --- |
| `--app` | Select another app repository, package directory, or app name under `--apps-root`. Defaults to the current directory. |
| `--apps-root` | Point to the directory containing the dependency repositories. Defaults to the parent of the custom app repository. |
| `--dependency` | Add a dependency missing from `required_apps`. Repeat the option to add more than one. |

Frappe is included automatically. The validator also reads `required_apps` from your app
and its dependencies. All of those repositories must be available under `--apps-root`,
with versions compatible with your app.

Prefer declaring dependencies in `hooks.py`, for example:

```python
required_apps = ["erpnext", "NovigoTechnology/nvg_base"]
```

If that declaration is incomplete, you can add dependencies for a validation run:

```bash
validate_override_annotations --app apps/blindaris --apps-root apps \
    --dependency nagus --dependency nvg_base
```

`--dependency` affects this check only; it does not install apps or change Frappe's configuration.

## Add the required annotation

Add these five fields to the overriding callable's docstring:

```python
def get_coa(doctype: str) -> list:
	"""
	APP: nagus
	HASH: cf4e3a0715d4fcb182d645c6fca72ae345c9761f
	REPO: https://github.com/NovigoTechnology/erpnext
	PATH: erpnext/accounts/utils.py
	METHOD: get_coa
	"""
	...
```

Replace the example values with those for your override:

| Field | What to put there |
| --- | --- |
| `APP` | The custom app containing your override, such as `nagus`. |
| `HASH` | The full 40-character upstream commit SHA that you reviewed. It does not have to match the checkout's current HEAD. |
| `REPO` | The upstream GitHub repository containing the original implementation. Match its checkout's `origin`, or the `owner/repository` in `required_apps` if no origin is configured. |
| `PATH` | The original implementation's file path, relative to the upstream repository root. |
| `METHOD` | The original function name or qualified method name, such as `Ledger.validate`. For inherited methods, use the class that defines the upstream method. |

Include each field once. You can keep explanatory prose in the docstring and arrange the
fields in any order. Prefer qualified method names over bare names such as `validate`.

The validator checks annotation completeness and references. `track_overrides` checks the
historical commit and compares implementations. Review upstream changes before updating `HASH`.

## Add it to pre-commit

In your custom app's `.pre-commit-config.yaml`, use a published tag or full commit SHA
containing these hooks:

```yaml
repos:
  - repo: https://github.com/NovigoTechnology/test_utils
    rev: <tag-or-commit>
    hooks:
      - id: validate_override_annotations
      - id: track_overrides
```

From that repository, install the hooks and run the annotation check:

```bash
pre-commit install
pre-commit run validate_override_annotations --all-files
```

No hook arguments are needed for the usual Bench layout. See the [tracking guide](actions/track_overrides.md)
for comparison options.

## Run in CI

Check out the custom app, Frappe, and every dependency into sibling directories such as
`apps/nagus`, `apps/frappe`, `apps/erpnext`, and `apps/nvg_base`. Select the dependency refs
that your app supports, install test_utils, and run from the workspace root:

```bash
validate_override_annotations --app apps/nagus --apps-root apps
```

Shallow checkouts are sufficient for this annotation check. Historical tracking has its own
[source and authentication requirements](actions/track_overrides.md#pre-commit-usage).

## Read the results and fix errors

The command ends with a summary, for example:

```text
validate_override_annotations: checked 7 override(s), 0 error(s)
```

Exit code `0` means the check passed. Exit code `1` means it found invalid annotations or
could not finish validation. Errors identify the affected source or hook registration.

| Reported problem | What to do |
| --- | --- |
| Missing annotation fields | Add all five fields to the overriding function or method's docstring. |
| App checkout not found | Check out the named repository under `--apps-root`. |
| An app is missing from `required_apps` | Correct the dependency declaration or add `--dependency` for this run. |
| `REPO` does not match the checkout | Check the annotation's upstream repository and the checkout's `origin` remote. |
| Incorrect `PATH` or `METHOD` | Use the file and defining class/function named in the error. The method reference must identify a single declaration. |
| Upstream method no longer exists | Review whether upstream renamed or removed the method, then update the customization and annotation. |
| Ambiguous or unresolved reference | Inspect the reported file and line. Use explicit imports and straightforward hook declarations in your app. An unsupported upstream pattern needs review before the check can pass. |

If the summary says `checked 0 override(s)`, confirm that the expected overrides are
registered in the supported hooks and inspect any preceding errors.
