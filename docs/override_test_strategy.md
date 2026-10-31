# Override test strategy

The suite protects observable override contracts and concrete regression families.
It does not enumerate every Python spelling supported by the analyzer. Application
checkouts are inspected as source; only bounded test fixtures run as Python oracles.

## Scope and counts

The 303 cases present before this PR remain unchanged, including validator,
tracker, CLI, and unrelated pre-commit tests. Compare against the PR merge base,
not the latest `main`, when checking that boundary.

The former 884-case collection contained 581 PR additions. The curated collection
has **476 cases: 303 preexisting and 173 PR cases**, including the new defects
found during this revision. The PR cases belong to these behavior suites:

| Key | Suite under `tests/pre_commit/` | Cases |
| --- | --- | ---: |
| W | `test_wildcard_controllers.py` | 20 |
| E | `test_export_provenance.py` | 23 |
| C | `test_callable_provenance.py` | 25 |
| K | `test_class_resolution.py` | 17 |
| P | `test_python_protocol_effects.py` | 48 |
| I | `test_imported_mutations.py` | 33 |
| Q | `test_containers_copy_identity.py` | 7 |

`bench`, `module`, and `resolver` stay importable from their original helper
modules. Nine added audit/review files were removed after their retained
invariants were moved into the suites above. No scenarios are hidden in loops,
optional markers, or a second test command.

## Requirement to regression map

The suite keys above identify the file containing each named test. A parameter
pair is retained only when its states have different observable behavior.

| Requirement or failure family | Concrete regression | Preservation evidence |
| --- | --- | --- |
| Wildcards select the active defining module and symbol | W `test_later_wildcard_resolves_the_replacement_class_and_its_actual_method`; `test_imported_upstream_member_preserves_its_defining_symbol` | W `test_later_wildcard_excluding_the_symbol_preserves_the_previous_class` |
| Dynamic exports explain the responsible import; inspection executes no app code | W `test_dynamic_exports_report_ambiguity_and_the_responsible_import` | W `test_inspecting_exports_never_executes_application_code` |
| Alias provenance survives unknown indexes, branch merges, and updates | E `test_unresolved_subscript_keeps_the_nested_export_alias`; `test_exhaustive_branches_keep_nested_container_aliases`; `test_conditional_update_keeps_the_unmodified_branch_alias` | E `test_unconditional_update_can_replace_an_alias_without_mutating_exports` |
| In-place changes keep the original object; concatenation creates a new outer object | E `test_augmented_alias_assignment_mutates_the_original_export_list`; Q `test_concatenated_containers_keep_the_same_nested_mutable_values` | Q `test_mutating_a_concatenated_copy_does_not_mutate_the_export_operand` |
| Argument binding preserves kwargs, positional-only names, and nonlocal aliases | E `test_kwargs_named_like_locals_are_still_forwarded_to_kwargs`; `test_positional_only_names_can_also_be_kwargs_keys`; `test_nonlocal_rebinding_keeps_the_outer_export_alias` | E `test_locally_shadowed_exports_do_not_mutate_module_exports` |
| Definition-time work and consumed work execute; stored bodies remain lazy | E `test_definition_time_defaults_execute_their_called_helper`; `test_eager_comprehensions_consume_their_generator_iterable` | E `test_storing_a_generator_does_not_execute_it`; `test_storing_a_callable_does_not_execute_its_body` |
| Deferred values preserve yielded references and bound receivers | E `test_consumption_keeps_references_yielded_by_a_generator`; `test_bound_generator_methods_keep_their_receiver_when_consumed` | P `test_generator_creation_does_not_advance_a_generator_body` |
| Typing guards and overloads depend on binding identity, not spelling | E `test_typing_dictionary_write_invalidates_the_false_guard`; C `test_uncertain_typing_receiver_cannot_hide_a_replacement_definition` | E `test_copied_false_typing_constant_survives_a_later_module_attribute_write`; C `test_captured_real_overload_keeps_its_identity_after_receiver_rebinding` |
| Metadata/default mutations invalidate the actual owning callable | C `test_modified_callable_metadata_cannot_certify_its_original_ast`; `test_keyword_default_alias_keeps_the_owning_definition`; `test_mutation_through_an_original_alias_invalidates_the_captured_default` | C `test_reading_or_copying_default_metadata_preserves_the_owner`; `test_operations_that_preserve_the_captured_default_keep_the_definition` |
| Class redefinitions and C3 preserve the captured declaring owner | K `test_circular_subclass_retains_its_original_base_after_import_returns`; C `test_mutated_defaults_follow_the_c3_selected_owner` | K `test_circular_snapshot_agrees_with_python_for_the_selected_import_entry`; C `test_captured_defaults_keep_the_original_definition_after_class_rebinding` |
| Imported mutations follow aliases and reexports without contaminating source/cache | I `test_mutation_through_a_parallel_reexport_alias_reaches_the_same_defining_function`; `test_importer_mutations_respect_partial_module_snapshot_and_cache_order` | I `test_imported_function_metadata_write_is_rejected_without_tainting_its_source`; `test_local_definition_after_importer_mutation_has_independent_metadata` |
| Opaque imported helpers, all wildcard exports, and inherited members retain mutations | I `test_opaque_imported_helper_cannot_certify_metadata_of_its_imported_argument`; `test_parallel_wildcards_preserve_alias_identity_from_each_known_export`; `test_wildcard_import_retains_mutations_through_other_known_exported_class_names` | I `test_imported_base_keeps_identity_and_applies_mutations_to_the_requested_member`; `test_known_readers_and_unrelated_wildcard_alias_attributes_preserve_metadata` |
| Copies retain shared children but reads and independent slots are safe | I `test_dict_constructor_copy_retains_nested_imported_default_aliases`; `test_imported_defaults_lookup_with_a_custom_key_keeps_possible_hash_side_effects` | Q `test_clearing_a_keyword_defaults_copy_does_not_mutate_the_imported_function`; I `test_replacing_a_slot_in_a_dict_constructor_copy_leaves_original_defaults_unchanged` |
| Constructors, descriptors, and class creation invoke their callbacks | P `test_constructor_initialization_executes_its_local_callback`; `test_constructor_allocation_executes_its_local_callback`; `test_descriptor_access_executes_the_getter`; `test_class_creation_initializes_named_descriptors`; `test_subclass_creation_executes_its_parent_hook`; `test_class_creation_executes_its_metaclass_constructor` | P `test_pure_object_protocols_keep_exports_provable`; `test_property_getter_returning_a_generator_keeps_its_body_lazy` |
| Context, conversion, hash, and sorting callbacks follow their invocation boundary | P `test_synchronous_context_entry_executes_its_callback`; `test_string_conversion_falls_back_to_repr`; `test_hashing_a_set_element_executes_its_hash_protocol`; `test_sorted_invokes_an_opaque_imported_key_callback`; `test_sorted_invokes_a_local_lambda_key` | P `test_pure_object_protocols_keep_exports_provable`; `test_string_conversion_does_not_invoke_an_inactive_repr` |
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
must agree with Python. Known pure or inactive paths must produce the expected
result. Raising `AmbiguousBinding` is therefore insufficient for positive tests.
Typing-module mutation examples are AST-only and never mutate real `typing`.

Run the normal collection and checks:

```bash
python -m pytest --collect-only -q
python -m pytest -q
```

This is a bounded regression set, not a claim to cover all Python execution
semantics. New tests should identify an observable failure absent from this map
or strengthen a positive contract, rather than grow another syntax catalog.
