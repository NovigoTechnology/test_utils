# Analyze overrides

`analyze_overrides` is the public integration interface for static Frappe
override analysis. It discovers registered overrides, validates their upstream
annotations and compares the reviewed implementation against the checked-out
dependency revision without importing either application.

`validate_override_annotations` and `track_overrides` remain the human-facing
pre-commit commands. Integrations should consume this command's JSON instead of
importing their internal modules or parsing terminal messages.

The discovery sources are `override_whitelisted_methods`,
`override_doctype_class`, `extend_doctype_class` and supported direct
module-function assignments. Direct assignments use
`discovery.kind: module_assignment`; hooks use `discovery.kind: hook`.

## Invocation

Prepare the custom app and all required dependencies as sibling Git checkouts:

```text
apps/
  frappe/
  erpnext/
  nagus/
```

Run:

```bash
analyze_overrides --app apps/nagus --apps-root apps
```

Options:

| Option | Purpose |
| --- | --- |
| `--app` | Repository or package containing the overrides. Required. |
| `--apps-root` | Directory containing dependency checkouts. Required. |
| `--dependency` | Additional app or `owner/repository`; repeat as needed. |
| `--revision APP=SHA` | Require a checkout to be at an exact 40-character commit. Repeat as needed. |
| `--output PATH` | Write JSON to a file. The default `-` writes JSON to stdout. |

The command reads the checked-out `HEAD` of every app once and records those
commits. A supplied `--revision` is a reproducibility assertion: the command
returns an incomplete result if the checkout is at a different commit.
All checkouts must be clean because static resolution reads their files; a
dirty checkout returns an incomplete setup result rather than attributing
uncommitted source to the recorded commit.

Exit code `0` means analysis completed, including expected findings such as a
confirmed change or invalid annotation. Exit code `2` means the result is
incomplete. Once argument parsing succeeds, stdout or `--output` contains one
JSON document; operational text does not share that stream.

## Contract version 1

Every document contains:

```json
{
  "schema_version": 1,
  "tool": {"name": "test_utils", "version": "1.2.5"},
  "subject": {
    "app": "nagus",
    "repository": "github.com/novigotechnology/nagus",
    "revision": "<commit>"
  },
  "sources": [],
  "results": [],
  "errors": [],
  "counts": {},
  "outcome": "completed"
}
```

`outcome` is one of:

| Outcome | Meaning |
| --- | --- |
| `completed` | Every discovered override was compared and no finding remains. |
| `completed_with_findings` | Analysis completed with an affected override or invalid annotation. |
| `incomplete` | At least one result was inconclusive, a technical error occurred, or setup failed. |

Each result contains:

- stable `override_id`;
- discovery source;
- owner app, repository, file, line, symbol, revision and body hash;
- resolved upstream app, repository, file, symbol and revisions when available;
- raw annotation fields, duplicates and normalized repository identity;
- change kind, upstream body hashes and method-level diff; and
- structured diagnostics with `code`, `kind` and a human-readable `message`.

`override_id` includes the owner repository, custom source location and symbol,
and the registered original identity. For a module assignment that identity is
the installation file plus its canonical imported target; the source spelling
remains available in `discovery.original_reference`. One replacement registered
for multiple upstream callables therefore has one stable ID per registration,
including when the upstream callable can no longer be resolved.

The machine-readable contract is shipped as
[`override_analysis_v1.schema.json`](../test_utils/schemas/override_analysis_v1.schema.json).
All properties marked as required there are present even when their value is
`null` or an empty string. Consumers should validate the complete document
before applying catalog or publication policy.

`classification` is one of:

| Classification | Meaning |
| --- | --- |
| `unaffected` | Baseline and target implementations are resolvable and equal. |
| `affected` | A valid baseline was resolved and the target changed or disappeared. |
| `invalid_annotation` | Required annotation metadata is missing or inconsistent. |
| `inconclusive` | Resolution was ambiguous or insufficient for a conclusion. |
| `technical_error` | Source access or another technical failure prevented comparison. |

A missing target is only `affected` after resolving a valid baseline. Missing
history, ambiguous symbols and inaccessible sources never become a clean or
confirmed result.

## Compatibility

Consumers must check `schema_version` before reading results and ignore unknown
fields in a supported version. Removing a field, changing its type or changing
the meaning of a classification requires a new schema version.

GitHub repositories are normalized to `github.com/owner/repository`.
Annotations retain their original `REPO` text and additionally expose
`repository_identity`, so `owner/repository`, HTTPS, SSH, SCP and Git variants
remain equivalent.
