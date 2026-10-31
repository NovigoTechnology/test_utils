# Override test strategy

The suite protects observable override contracts and concrete regression families.
It does not enumerate every Python spelling supported by the analyzer. Application
checkouts are inspected as source; only bounded test fixtures run as Python oracles.

## Scope and counts

The collection retains all 303 cases present before this PR, including validator,
tracker, CLI, and unrelated pre-commit tests. The curation did not remove or
suppress preexisting cases. Compare against the PR merge base, not the latest
`main`, when checking that boundary.

The former 884-case collection contained 581 PR additions. The curated collection
has **516 cases: 303 preexisting and 213 PR cases**, including the new defects
found during this revision. The PR cases belong to these behavior suites:

| Key | Suite under `tests/pre_commit/` | Cases |
| --- | --- | ---: |
| W | `test_wildcard_controllers.py` | 20 |
| E | `test_export_provenance.py` | 46 |
| C | `test_callable_provenance.py` | 28 |
| K | `test_class_resolution.py` | 20 |
| P | `test_python_protocol_effects.py` | 58 |
| I | `test_imported_mutations.py` | 34 |
| Q | `test_containers_copy_identity.py` | 7 |

`bench`, `module`, and `resolver` stay importable from their original helper
modules. Nine added audit/review files were removed after their retained
invariants were moved into the suites above. No scenarios are hidden in loops,
optional markers, or a second test command.

## Declared analysis boundary

Native scalar dictionary keys and native tuples of primitive keys are supported.
A union of primitive scalars remains safe for native hashing, but is not an exact key. Its
abstract alternatives must not be mistaken for a literal tuple. Likewise, a
conversion with an unknown scalar result does not establish a particular string
key. Literal `None` remains a valid key, distinct from an unknown entry.

Application-defined dictionary keys, application-defined stored keys, and tuples
containing them are explicitly unproven. The analyzer reports ambiguity rather
than partially simulating arbitrary or reentrant dictionary hash/equality probes.
This refusal also applies when an owned runtime example happens to have no side
effects. It is not a blanket refusal of set conversions or known local hash
callbacks, which retain their own protocol analysis. Custom prepared class
namespaces have a corresponding uncertainty boundary; a proven native prepared
dictionary remains supported.

Evaluation order and captured values are part of the contract. Boolean operands
are evaluated and truth-tested in Python order; proven short circuits stop, while
unknown choices join possible states and values. The selected operand retains
its callable, deferred, and ownership information. A callable is captured before
its arguments run, and a container receiver is captured before its key or index
is evaluated. `setdefault` evaluates its arguments but returns the existing entry
when present; removing an entry with `pop` changes that later selection.

Decorator expressions are captured in definition order. Class preparation runs
before its body, and decorators are applied after construction, from inner to
outer. Overload classification uses those evaluated decorator values rather than
an earlier scan of their names, including attribute writes and dictionary writes
inside a decorator factory's return expression. A previously captured real
overload alias keeps its identity after a receiver is rebound. Special-method lookup uses current
class declarations; dictionary entries named like methods remain ordinary data.

## Requirement to regression map

The suite keys above identify the file containing each named test. A parameter
pair is retained only when its states have different observable behavior.

| Requirement or failure family | Concrete regression | Preservation evidence |
| --- | --- | --- |
| Wildcards select the active defining module and symbol | W `test_later_wildcard_resolves_the_replacement_class_and_its_actual_method`; `test_imported_upstream_member_preserves_its_defining_symbol` | W `test_later_wildcard_excluding_the_symbol_preserves_the_previous_class` |
| Dynamic exports explain the responsible import; inspection executes no app code | W `test_dynamic_exports_report_ambiguity_and_the_responsible_import` | W `test_inspecting_exports_never_executes_application_code` |
| Alias provenance survives unknown indexes, branch merges, and updates | E `test_unresolved_subscript_keeps_the_nested_export_alias`; `test_exhaustive_branches_keep_nested_container_aliases`; `test_conditional_update_keeps_the_unmodified_branch_alias` | E `test_unconditional_update_can_replace_an_alias_without_mutating_exports` |
| Dictionary key purity and key exactness are separate; application keys are explicitly unproven | E `test_abstract_scalar_alternatives_are_not_an_exact_tuple_key`; `test_abstract_scalar_keys_preserve_possible_assignment_targets`; `test_application_dictionary_keys_are_explicitly_unproven` | E `test_native_tuple_keys_remain_provable` |
| Calls and mappings use the receiver captured before later argument/key effects | E `test_callable_is_captured_before_its_argument_rebinds_the_name`; `test_native_method_receiver_is_captured_before_argument_side_effects`; `test_mapping_keeps_the_receiver_captured_before_key_evaluation` | E `test_boolean_short_circuit_preserves_the_selected_callable` |
| In-place changes keep the original object; concatenation creates a new outer object | E `test_augmented_alias_assignment_mutates_the_original_export_list`; Q `test_concatenated_containers_keep_the_same_nested_mutable_values` | Q `test_mutating_a_concatenated_copy_does_not_mutate_the_export_operand` |
| Argument binding preserves kwargs, positional-only names, and nonlocal aliases | E `test_kwargs_named_like_locals_are_still_forwarded_to_kwargs`; `test_positional_only_names_can_also_be_kwargs_keys`; `test_nonlocal_rebinding_keeps_the_outer_export_alias` | E `test_locally_shadowed_exports_do_not_mutate_module_exports` |
| Definition-time work and consumed work execute; stored bodies remain lazy | E `test_definition_time_defaults_execute_their_called_helper`; `test_eager_comprehensions_consume_their_generator_iterable` | E `test_storing_a_generator_does_not_execute_it`; `test_storing_a_callable_does_not_execute_its_body` |
| Deferred values preserve yielded references and bound receivers | E `test_consumption_keeps_references_yielded_by_a_generator`; `test_bound_generator_methods_keep_their_receiver_when_consumed` | P `test_generator_creation_does_not_advance_a_generator_body` |
| Existing entries, native dictionary pairs, discarded defaults, `None` keys, and removed entries preserve deferred selection | E `test_setdefault_returns_the_existing_deferred_value_without_consuming_the_other`; `test_setdefault_selects_the_existing_value_from_native_dictionary_pairs`; `test_pop_removes_the_old_entry_before_setdefault_selects_a_new_default` | E `test_setdefault_with_a_literal_none_key_keeps_the_existing_iterator` |
| Typing guards and overloads depend on binding identity, not spelling | E `test_typing_dictionary_write_invalidates_the_false_guard`; C `test_uncertain_typing_receiver_cannot_hide_a_replacement_definition` | E `test_copied_false_typing_constant_survives_a_later_module_attribute_write`; C `test_captured_real_overload_keeps_its_identity_after_receiver_rebinding` |
| Decorator application and overload classification use evaluated values in definition order | P `test_function_decorators_apply_inner_before_outer`; `test_class_body_runs_before_its_decorator_is_applied`; C `test_overload_classification_uses_the_already_evaluated_decorator_values` | P `test_decorator_expression_is_captured_before_the_class_body_rebinds_it` |
| Metadata/default mutations invalidate the actual owning callable | C `test_modified_callable_metadata_cannot_certify_its_original_ast`; `test_keyword_default_alias_keeps_the_owning_definition`; `test_mutation_through_an_original_alias_invalidates_the_captured_default` | C `test_reading_or_copying_default_metadata_preserves_the_owner`; `test_operations_that_preserve_the_captured_default_keep_the_definition` |
| Class redefinitions and C3 preserve the captured declaring owner | K `test_circular_subclass_retains_its_original_base_after_import_returns`; C `test_mutated_defaults_follow_the_c3_selected_owner` | K `test_circular_snapshot_agrees_with_python_for_the_selected_import_entry`; C `test_captured_defaults_keep_the_original_definition_after_class_rebinding` |
| Imported mutations follow aliases and reexports without contaminating source/cache | I `test_mutation_through_a_parallel_reexport_alias_reaches_the_same_defining_function`; `test_importer_mutations_respect_partial_module_snapshot_and_cache_order` | I `test_imported_function_metadata_write_is_rejected_without_tainting_its_source`; `test_local_definition_after_importer_mutation_has_independent_metadata` |
| Opaque imported helpers, all wildcard exports, and inherited members retain mutations | I `test_opaque_imported_helper_cannot_certify_metadata_of_its_imported_argument`; `test_parallel_wildcards_preserve_alias_identity_from_each_known_export`; `test_wildcard_import_retains_mutations_through_other_known_exported_class_names` | I `test_imported_base_keeps_identity_and_applies_mutations_to_the_requested_member`; `test_known_readers_and_unrelated_wildcard_alias_attributes_preserve_metadata` |
| Copies retain shared children but reads and independent slots are safe | I `test_dict_constructor_copy_retains_nested_imported_default_aliases`; `test_imported_defaults_lookup_with_a_custom_key_keeps_possible_hash_side_effects` | Q `test_clearing_a_keyword_defaults_copy_does_not_mutate_the_imported_function`; I `test_replacing_a_slot_in_a_dict_constructor_copy_leaves_original_defaults_unchanged` |
| Constructors, descriptors, and class creation invoke their callbacks | P `test_constructor_initialization_executes_its_local_callback`; `test_constructor_allocation_executes_its_local_callback`; `test_descriptor_access_executes_the_getter`; `test_class_creation_initializes_named_descriptors`; `test_subclass_creation_executes_its_parent_hook`; `test_class_creation_executes_its_metaclass_constructor` | P `test_pure_object_protocols_keep_exports_provable`; `test_property_getter_returning_a_generator_keeps_its_body_lazy` |
| Context, conversion, and sorting callbacks follow their invocation boundary | P `test_synchronous_context_entry_executes_its_callback`; `test_string_conversion_falls_back_to_repr`; `test_sorted_invokes_an_opaque_imported_key_callback`; `test_sorted_invokes_a_local_lambda_key` | P `test_pure_object_protocols_keep_exports_provable`; `test_string_conversion_does_not_invoke_an_inactive_repr` |
| Iteration acquires once and async work is awaited through the selected protocol | P `test_generator_creation_acquires_the_outer_iterator_immediately`; `test_async_iteration_follows_the_iterator_returned_by_aiter` | P `test_generator_outer_iterator_is_captured_without_reacquiring_it`; `test_async_iteration_does_not_invoke_sync_counterparts` |
| Returned protocol values, reflected/in-place order, and short-circuiting remain observable | P `test_binary_result_keeps_the_returned_truth_protocol`; `test_chained_comparison_truth_tests_intermediate_result`; `test_augmented_assignment_uses_fallback_for_notimplemented` | P `test_chained_comparison_stops_on_the_intermediate_truth_result`; `test_augmented_assignment_does_not_call_fallback_after_successful_inplace` |
| Exception matching runs after the try prefix, only on the relevant path | P `test_exception_type_expression_is_evaluated_after_the_try_body`; `test_unmatched_exception_types_keep_effects_for_later_type_expressions`; `test_exception_prefix_retains_container_aliases_before_a_later_update` | P `test_exception_type_expression_is_not_evaluated_without_an_exception` |

## Curating and running the suite

The old products of 32 container routes by three mutations, every dictionary
mutator by function/method scope, and every callback spelling were removed.
One representative now protects each failure boundary above. Copy identity,
owner identity, invocation versus storage, and inactive protocol selection remain
separate because conflating them previously caused real defects.

The remaining small parameters express real states: included/excluded exports,
valid/invalid annotation metadata, cache initialization order, selected/inactive
callbacks, lazy/consumed filters, and subscript assignment/deletion. They do not
multiply unrelated syntax dimensions.

A mutating or unresolved path may be rejected as ambiguous; any proven result
must agree with Python. Pure or inactive paths within the declared boundary must
produce the expected result. Raising `AmbiguousBinding` is therefore insufficient
for positive tests; explicit unsupported-key and custom-namespace regressions
separately require refusal.
Typing-module mutation examples are AST-only and never mutate real `typing`.

Run the normal collection and checks:

```bash
python -m pytest --collect-only -q
python -m pytest -q
```

This is a bounded regression set, not a claim to cover all Python execution
semantics. New tests should identify an observable failure absent from this map
or strengthen a positive contract, rather than grow another syntax catalog.
