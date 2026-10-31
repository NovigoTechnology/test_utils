# Override test strategy

Tests are organized around observable behavior rather than successive review
numbers. A regression should identify the binding, mutation, or execution
boundary it protects. Adding another syntax spelling does not automatically
justify crossing it with every existing scenario.

## Coverage map

All filenames below are under `tests/pre_commit/`.

| Behavior | Suites | Required evidence |
| --- | --- | --- |
| Export aliases, containers, arguments, returned values, and closures | `test_export_provenance.py` | Compare bounded, owned snippets with Python; every recovered alias route has a representative mutation. |
| Callable metadata, default dictionaries, copies, decorators, and owning definitions | `test_callable_provenance.py`, `test_captured_default_ownership.py` | Mutations invalidate the correct owner; reads, independent copies, unrelated methods, and rebinding remain resolvable. |
| Implicit callbacks, iteration, lazy creation, async protocols, and C3 dispatch | `test_python_protocol_effects.py`, `test_python_protocol_regressions.py` | Compare invoked and inactive protocols with Python; inactive counterparts must not produce false ambiguity. |
| Circular imports, partial snapshots, inheritance, and same-file metadata identity | `test_class_resolution.py` | Verify the captured declaring class, cache order, C3 precedence, and validator diagnostics. |
| Imported-object mutation and conditional container updates | `test_imported_mutations.py`, `test_container_branch_aliases.py` | Preserve importer-local provenance and aliases that remain possible on another branch. |
| Wildcard selection, reexports, legacy names, and validator/tracker integration | `test_wildcard_controllers.py`, `test_wildcard_review_regressions.py`, `test_override_wildcard_review.py` | Check defining module/symbol, import location in errors, and actual inventory/tracking behavior. |
| Lexical scope, typing guards, decorator context, and active callable selection | `test_override_scope_review.py`, `test_override_execution_review.py`, `test_export_effects_regressions.py`, `test_scope_contracts.py`, `test_resolver_independent_audit.py` | Preserve unique regressions for global/nonlocal lookup, typing aliases, definition-time effects, and controller/member lookup. |

The existing `bench`, `module`, and `resolver` helpers stay importable from
`test_validate_override_annotations.py` and `test_wildcard_controllers.py`.
Tests predating this PR and tests unrelated to overrides are unchanged.

## Matrix and duplication policy

The consolidation removes 110 collected cases from the previously existing
affected suites: 545 become 435. New regressions for newly reported defects are
counted separately. The reduction comes from these coverage decisions:

- Keep all 32 container provenance routes with one representative `append`.
  Exercise slice assignment and augmented assignment on three representative
  routes, giving 38 cases instead of the full 96-case product.
- Test all nine callable-container operations with a named function. Sample a
  lambda on insertion and retrieval: 11 cases replace 18. Lambda creation,
  arguments, and captured defaults retain their separate regressions.
- Test all 15 keyword-default mutations on a function, then sample direct,
  projected, and helper-returned method owners: 18 cases replace 30. Metadata
  reads/copies similarly use eight cases instead of 12. General callable
  metadata writes use nine cases instead of 14.
- Remove repeated generator creation/consumption, constructor/context manager,
  default capture, nonlocal alias, and class-identity examples once a stronger
  runtime oracle covers them. Remove boolean parameter combinations when the
  snippet never uses that parameter.

Remaining products represent interactions that can select different behavior:
reflected ordering covers each operator and its inactive counterpart; protocol
selection covers invocation versus laziness; circular resolution covers both
import forms and both cache orders. Integration tests remain separate when
they assert diagnostics, inventory entries, or declaring module identity that
an AST unit test cannot establish.

## Oracles and false positives

Runtime oracles execute only bounded snippets owned by the tests. Application
modules are inspected as source, never imported. Examples involving mutations
of `typing` itself are AST-only.

A mutating or genuinely uncertain path may be rejected as ambiguous, but any
proven result must match Python. A known inactive or independent operation must
return the expected export set: ordinary protocols, stored generators, cyclic
container bookkeeping, metadata reads, and independent copies cannot pass
merely by raising `AmbiguousBinding`. Conditional execution tests retain
ambiguity when the analyzer cannot prove which branch runs.

Run the core behavior suites with:

```bash
python -m pytest -q tests/pre_commit/test_export_provenance.py \
  tests/pre_commit/test_callable_provenance.py \
  tests/pre_commit/test_python_protocol_effects.py
```

Run `python -m pytest -q tests/pre_commit` for validator/tracker integration and
the other pre-commit checks. Keep new regressions individually collected; do not
hide scenarios in loops or behind an optional test marker to reduce the count.
