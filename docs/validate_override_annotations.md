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
Assignments, deletions and `setattr` changes to class members are included in the method
inventory and invalidate the affected implementation. Changes to a function's `__code__`,
defaults or annotation docstring also invalidate its old source definition. An unrelated
method with the same name in another scope does not replace a module-level function.
Mutating `__kwdefaults__` through a dictionary alias, container projection or mutating
method also invalidates the owning function. A shallow copy has independent dictionary
slots, while mutable default values reached through that copy retain their original identity.
An overload declaration is skipped only when its decorator is proven to be the current
absolute `typing.overload` or `typing_extensions.overload` binding at definition time.
Local decorators with those names, rebound aliases and modified typing attributes cannot
hide the active implementation. Projecting `overload` from an uncertain module receiver
does not establish that proof. Legacy leaf-name lookup considers active class bindings.
Plain literal data attributes redeclared in both classes do not require method annotations.
Wildcard imports are resolved in declaration order. An explicit unconditional definition
after a wildcard import establishes the active binding. For a later wildcard, local source
checkouts supply static `__all__` exports or public declarations/re-exports when `__all__`
is absent. An import that excludes the name preserves its earlier definition; an import that
replaces it follows the actual defining module for `PATH` and `METHOD` validation.
Imported class members also retain their defining module and callable name. Inherited
methods use the class selected when the subclass was defined, even if a later import
replaces that class's name in the module.
Inheritance and method ownership distinguish separate class definitions even when they
share a qualified name. A redefinition such as `class Base(Base)` therefore keeps its
earlier base rather than being mistaken for a cycle.
Inherited callbacks and qualified members use C3 declarations, so a class that only inherits
a member does not hide a later class's own declaration in the same hierarchy.
Conditional base identities retain candidate callbacks and make the resulting class uncertain.
Member resolution retains findings from the enclosing module, including local decorators
that replace methods. The method inventory uses the same runtime-filtered class body as
member lookup: a method declared only under a proven false `TYPE_CHECKING` guard does not
become an override. Nested class lookup skips enclosing class namespaces, as Python does.

Circular re-exports can resolve an already defined class from the partially initialized
module, as with Frappe's `File` controller and `frappe.core.api.file`. Proven
class identities retain that import snapshot when their bases are resolved later. If a
captured method and a later definition share both `PATH` and qualified `METHOD`, those
annotation fields cannot distinguish them; validation reports an ambiguous method.
Proven `typing.TYPE_CHECKING` imports do not become runtime exports. This requires an absolute
standard typing import whose binding has not subsequently changed; a relative `.typing`
import or an intervening wildcard cannot prove the guard false.
Writes or deletions of the typing module's `TYPE_CHECKING` attribute also invalidate the
proof through its known module aliases. Importing that same module again does not reset
the attribute; an already copied false `TYPE_CHECKING` constant keeps its value.
This includes helper-local imports, aliases passed as parameters, and writes made with
`setattr` or `delattr`, including known aliases of those builtins.
Mutations or rebindings of export lists inside called local helpers, and unsupported
unpacking assignments to `__all__` or its constant aliases, make exports ambiguous.
The analysis includes bare local decorators, conditional wildcard imports, and mutations
through nested container aliases. Annotation-only declarations such as
`__all__: list[str]` do not rebind an existing export list.
Executed helpers that rebind public names declared `global` also make those names
ambiguous, even without `__all__`; a later explicit definition establishes a new binding.
Class bodies also honor their `global` declarations. Container aliases follow lexical
scope through parameters, closures, `nonlocal` assignments, branches and loops. A helper
that creates its own local `__all__` does not mutate the module's export list.
Container identity is retained across conditional alias rebindings and captured defaults.
Positional and keyword-only defaults capture their values when the function is defined;
reassigning a name later does not change an earlier default or make mutations of an old
export list affect its replacement.
Unknown indices and joins retain possible element identities. Container transformations,
starred arguments, slices, views and supported mutation methods preserve reachable aliases
and callbacks; an unsupported value operation does not prove its aliases absent.
Joins preserve implicit method receivers and terminate for cyclic container graphs.
Keyword binding distinguishes positional-only, keyword-accepting and local variable names.
Dynamic namespace operations reached through helpers or builtin aliases are rejected.
`vars(value)` is exempt only when the value is proven to be a declared class, including
known class aliases. `vars(sys.modules[__name__])` and unknown objects remain ambiguous.
Known local decorators that replace or modify their argument make the decorated
definition ambiguous; merely writing `return cls` is insufficient when `cls` is rebound
or its attributes are assigned through an alias. Every reachable successful exit must
return the unchanged argument; an implicit `None` return is not an identity decorator.
Reading the argument's `__name__`, `__qualname__` or `__module__` metadata does not modify it.
Uncalled function bodies and ordinary helper-local
assignments do not rebind public names.

Creating a coroutine, generator function result, or generator expression defers its body.
A generator expression evaluates its outermost iterable and acquires its iterator at creation;
advancement and filter truth tests remain deferred. The captured iterator is not acquired
again during advancement. Effects are analyzed
at awaits, iteration, `yield from`, known consumers such as `list`/`next`/`asyncio.run`,
and iterator advancement. Passing a deferred value to an unknown callable conservatively
includes possible consumption; merely storing it or applying `iter` does not consume it.
Eager comprehensions consume every iterable. Keyword arguments are analyzed with the same
rules as positional arguments, while storage-only `dict(value=generator)` remains lazy.
`iter(callable, sentinel)` defers the callback and sentinel comparison until advancement.
Async iteration acquires `__aiter__` and awaits `__anext__`; async context managers await
`__aenter__` and `__aexit__`. These protocols also apply to async comprehensions and
generator expressions. Subscript writes and deletions inspect `__setitem__` and
`__delitem__`, and reflected ordering uses the inverse comparison method on the right operand.
Known local constructors, methods and protocol callbacks are inspected, including callbacks
passed to operations such as `sorted(key=...)`. An opaque callback executed while a known
`__all__` exists makes the export list ambiguous, even when the call has no arguments.
Dynamic exports, unavailable
source, conditional replacements, and unresolved cycles produce an **ambiguous resolution**
diagnostic naming the responsible import and location, rather than claiming a controller
was removed. This analysis never imports or executes application modules.

This is a source analysis contract, not a proof for arbitrary Python execution. When a
module has no `__all__`, public names come from its declarations and locally visible
binding effects. Uninspected external code that secretly rewrites the importing module's
namespace, including reflective or cyclic dependency side effects, is outside that model.
Use runtime integration tests for such code; a passing annotation check does not establish
the absence of every possible external side effect. The regression suite compares controlled
examples against Python for the modeled execution, aliasing and lookup rules.

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
