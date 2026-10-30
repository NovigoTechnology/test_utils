# Validate override annotations

`validate_override_annotations` detects Frappe overrides missing upstream tracking metadata.
It reads Python source with AST, without importing apps, starting a site, or accessing the network.
Use it alongside [`track_overrides`](actions/track_overrides.md), which compares annotated
implementations against upstream history.

## Local use

In a Bench layout, dependencies are sibling repositories:

```text
apps/
  frappe/frappe/
  erpnext/erpnext/
  nvg_base/nvg_base/
  nagus/nagus/hooks.py
```

Run from the custom app repository:

```bash
validate_override_annotations --app nagus --apps-root ..
# Equivalent defaults when running from apps/nagus:
validate_override_annotations
```

`--app` accepts a repository path, package path, or app name under `--apps-root`.
When omitted, `--apps-root` defaults to the parent of the custom app repository.
The command exits with 0 when validation passes, or 1 for missing/invalid annotations,
unresolved references, missing checkouts, or unsupported hook configuration.
Errors include the custom file, line, symbol, and resolved upstream symbol.

## Discovery

The validator reads `required_apps` transitively from the custom app and each dependency's
`hooks.py`, and includes `frappe` implicitly. Entries can be app names (`erpnext`) or GitHub repository names
(`NovigoTechnology/nvg_base`). Repository names with hyphens are normalized to package
names with underscores. All these apps must be checked out under `--apps-root`.
Every transitive dependency must also be available under `--apps-root`. If an app's
`required_apps` declaration is incomplete, explicitly add resolution dependencies with
repeatable `--dependency` arguments, for example:

```bash
validate_override_annotations --app blindaris --apps-root .. --dependency nagus --dependency nvg_base
```

These arguments supplement static analysis; they do not change runtime Frappe hooks.
Prefer keeping the application's `required_apps` declaration accurate.

It inspects these hooks:

- `override_whitelisted_methods`: resolves each original function and its replacement.
- `override_doctype_class`: resolves registered classes and their inherited methods.
- `extend_doctype_class`: also finds the upstream DocType controller, so mixins without
  an explicit upstream base class are checked. When a replacement is registered with
  `override_doctype_class`, extensions are composed with that replacement first. A replacement
  in the inspected app takes precedence over dependencies, and the last entry in its hook wins.
  Conflicting dependency replacements are reported as ambiguous because installed app order
  cannot be inferred from source checkouts.

Extension mixins from a dependency are included before the controller in the C3 hierarchy.
Mixins within an app follow reverse registration order, as in Frappe. When several dependency
apps register extensions for the same DocType, validation reports an unresolved composition:
the checkouts alone do not establish their installed app order.

Imported bases, aliases, relative imports, re-exports, and multiple inheritance use
AST resolution. Methods are matched using Python's C3 inheritance order. Functions and
methods may be async or have return annotations. Overload declarations from typing or
typing_extensions are excluded when selecting the concrete implementation; an overload-only
or ambiguous definition is reported as unresolved. Extension/controller composition uses C3,
while replacement classes use only their declared bases. New helper methods absent from the
upstream hierarchy do not need annotations, including properties with getters and setters.
An overriding descriptor that cannot resolve to one concrete callable is reported as unresolved;
validation continues for the remaining methods. Resolution stops at the first upstream class
declaring an attribute, including descriptors and assigned attributes; it never falls through
to an ancestor hidden by that declaration. Class-level callable aliases such as
`validate = custom_validate`, and reassigned/deleted callable names, are reported as unresolved
when they replace an upstream method. Define the overriding method explicitly with `def` or
`async def` and put its metadata on that definition. A registered function that is rebound
after its definition cannot reuse the old definition's metadata.
Plain literal data attributes redeclared in both classes do not require method annotations.
Wildcard imports are resolved in declaration order. An explicit unconditional definition
after a wildcard import establishes the active binding. For a later wildcard, local source
checkouts supply static `__all__` exports or public declarations/re-exports when `__all__`
is absent. An import that excludes the name preserves its earlier definition; an import that
replaces it follows the actual defining module for `PATH` and `METHOD` validation.
Imported class members also retain their defining module and callable name. Inherited
methods use the class selected when the subclass was defined, even if a later import
replaces that class's name in the module.

Circular re-exports can resolve an already defined class from the partially initialized
module, as with Frappe's `File` controller and `frappe.core.api.file`. Proven
`typing.TYPE_CHECKING` imports do not become runtime exports. This requires an absolute
standard typing import whose binding has not subsequently changed; a relative `.typing`
import or an intervening wildcard cannot prove the guard false.
Mutations or rebindings of export lists inside called local helpers, and unsupported
unpacking assignments to `__all__` or its constant aliases, make exports ambiguous.
Uncalled function bodies do not change the export list.
Dynamic exports, unavailable
source, conditional replacements, and unresolved cycles produce an **ambiguous resolution**
diagnostic naming the responsible import and location, rather than claiming a controller
was removed. This analysis never imports or executes application modules.

The source-only extractor used by historical tracking has no checkout context for imported
modules. It can resolve definitions after earlier wildcards; a later wildcard whose exports
cannot be inspected remains unresolved and is reported for review.
Imported aliases for `abc.ABC`, `typing.Protocol`,
`typing_extensions.Protocol`, and `builtins.object` do not require application checkouts.
Application classes imported or declared under names such as `ABC` or `Protocol` remain part
of the hierarchy; only resolved standard-library identities are excluded.
A previously annotated method that no longer
exists upstream is an error. Without an earlier annotation, a removed method cannot be
distinguished from a new helper.

`doc_events`, unrelated functions, and mock-based tests are not discovery sources.
The validator reads only registered implementations and the source needed to resolve them.

Hooks support static strings, dictionaries, lists/tuples, references to earlier static
variables, concatenation, dictionary unpacking/union, and `+=`/`|=` assignments.
Tuple concatenation creates a new tuple, preserving earlier alias values; list `+=` and
dictionary `|=` preserve in-place updates shared by aliases.
Dynamic hook construction using application calls, conditional assignments, item mutations,
or `.update()` is unsupported and must be rewritten as static assignments. This includes
mutations through aliases and nested containers, before or after assigning the hook.
Unsupported operations touching a hook container invalidate its aliases, rather than
silently yielding an empty registration. Conditional reassignment or deletion of a scalar
alias invalidates that binding too. Hook-binding imports and wildcard imports in `hooks.py`
are rejected; imports cannot reuse an earlier static binding. Application code is never
executed to obtain hook values. Dynamically computed class bases cannot be resolved;
use explicit imports and static base classes.

Definition-time effects also count: decorators, default argument values (including lambdas),
and executable class bodies can change a hook container. Unsupported effects invalidate the
container instead of silently producing an empty registry. Class-local aliases of a known
hook container are conservatively unsupported. Deferred function and method bodies are not
treated as executed merely because they are defined. Conditional hook bindings, including
match captures and exception aliases, are rejected even when no earlier hook value exists.

## Required docstring

Put all five fields in the overriding callable's own docstring:

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

- `APP` must identify the custom app containing the override (`nagus` here).
- `HASH` must contain exactly 40 hexadecimal characters. This validator checks the SHA
  format; historical commit availability and implementation drift are handled by
  `track_overrides`. The hash need not equal the checkout's current HEAD.
- `REPO` must identify a GitHub repository supported by the trackers and match the upstream
  checkout's `origin` remote. HTTPS, SSH, SCP syntax,
  `.git` suffixes, and trailing slashes are normalized for comparison. If no origin is
  configured, an `owner/repository` entry in `required_apps` supplies a GitHub fallback.
  A short app name alone does not identify a repository; configure its origin.
  Repository mismatch errors show the normalized identity without origin credentials.
- `PATH` must equal the repository-relative path of the resolved upstream implementation.
- `METHOD` must identify that implementation within the file. Use `Ledger.validate` for
  class methods. Legacy bare names such as `validate` are accepted only when unambiguous.
  Methods inherited from another upstream class must name the class that defines them.

Fields may appear in any order, with prose between them. Validation and both trackers
share the same field parser. Each field must appear exactly once. Annotations in comments, module docstrings, or class
docstrings do not cover a function or method.

## Pre-commit

Replace `<tag-or-commit>` with a release or full commit SHA containing the new hook:

```yaml
- repo: https://github.com/NovigoTechnology/test_utils
  rev: <tag-or-commit>
  hooks:
    - id: validate_override_annotations
      args: ["--app", "nagus", "--apps-root", ".."]
      pass_filenames: false
      always_run: true

    - id: track_overrides
      args: ["--app", "nagus", "--base-branch", "nvg-develop"]
      pass_filenames: false
      always_run: true
```

The new hook discovers untracked overrides; `track_overrides` detects changes to the
upstream implementations of overrides that already have metadata. Both support qualified
`METHOD` values. Tracking reports upstream method removal and unresolved symbols, rather
than treating a missing method as unchanged.

## CI checkouts

CI must explicitly check out the custom app, Frappe, and every direct or transitive `required_apps` dependency
into sibling directories before running validation. For example, if the custom checkout
is `apps/nagus`, use checkout paths `apps/frappe`, `apps/erpnext`, and `apps/nvg_base`, then:

```bash
validate_override_annotations --app apps/nagus --apps-root apps
```

Select dependency repositories and refs in the workflow to match the supported app versions.
The validator uses the checked-out source and origin; it does not infer a branch from an
app name, fetch dependencies, or select a public repository in place of a checked-out fork.
Shallow checkouts work because annotation validation does not require historical commits.
