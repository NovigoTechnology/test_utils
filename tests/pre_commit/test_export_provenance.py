"""Export provenance, lexical ownership, and execution-boundary regressions."""

import ast

import pytest
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, runtime_body, static_exports


def assert_exports(operation, *, changed):
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	exec(compile(source, "<export-contract>", "exec"), namespace)
	expected = {"helper", "File"} if changed else {"helper"}
	assert set(namespace["__all__"]) == expected
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		assert changed, "An inactive or independent operation must keep exports provable"
	else:
		assert proven == expected


def test_unresolved_subscript_keeps_the_nested_export_alias():
	assert_exports("box = [__all__]\nindex = 0\nbox[index].append('File')", changed=True)


@pytest.mark.parametrize("operation", ["mapping[Key()]", "mapping[Key()] = 1", "del mapping[Key()]"])
def test_native_mapping_access_refuses_unproven_application_key_callbacks(operation):
	assert_exports(
		"class Key:\n    def __hash__(self):\n        __all__.append('File')\n        return 0\n"
		"    def __eq__(self, other): return other == 0\nmapping = {0: 0}\n" + operation,
		changed=True,
	)


def test_unproven_key_callbacks_do_not_certify_later_alias_uses():
	assert_exports(
		"alias = []\nclass Key:\n    def __hash__(self):\n        global alias\n        alias = __all__\n        return 0\n"
		"    def __eq__(self, other): return other == 0\nmapping = {0: 0}\ndel mapping[Key()]\nalias.append('File')",
		changed=True,
	)


@pytest.mark.parametrize("operator, returned", [("and", "True"), ("or", "False")])
def test_boolean_truth_effects_happen_before_the_selected_operand(operator, returned):
	assert_exports(
		"alias = []\nclass Guard:\n    def __bool__(self):\n        global alias\n        alias = __all__\n"
		f"        return {returned}\nGuard() {operator} alias.append('File')",
		changed=True,
	)


def test_boolean_short_circuit_preserves_the_selected_callable():
	assert_exports(
		"def configure():\n    __all__.append('File')\ndef clean(): pass\n"
		"disabled = False\nselected = disabled and configure or clean\nselected()",
		changed=False,
	)


@pytest.mark.parametrize(
	"existing, default, changed", [("configure()", "iter(())", True), ("iter(())", "configure()", False)]
)
def test_setdefault_returns_the_existing_deferred_value_without_consuming_the_other(
	existing, default, changed
):
	assert_exports(
		"def configure():\n    __all__.append('File')\n    yield 0\n"
		f"box = {{'pending': {existing}}}\npending = box.setdefault('pending', {default})\nnext(pending, None)",
		changed=changed,
	)


def test_setdefault_unknown_key_keeps_the_possible_existing_export_alias():
	assert_exports(
		"class Key:\n    def __hash__(self): return 0\n    def __eq__(self, other): return other == 0\n"
		"box = {0: __all__}\nbox.setdefault(Key(), []).append('File')",
		changed=True,
	)


def test_native_method_receiver_is_captured_before_argument_side_effects():
	assert_exports(
		"box = {'value': __all__}\ndef replacement():\n    global box\n    box = {}\n    return []\n"
		"box.setdefault('value', replacement()).append('File')",
		changed=True,
	)


def test_mapping_keeps_the_receiver_captured_before_key_evaluation():
	assert_exports(
		"def key():\n    global mapping\n    mapping = {0: []}\n    return 0\n"
		"mapping = {0: __all__}\nmapping[key()].append('File')",
		changed=True,
	)


def test_application_dictionary_keys_are_explicitly_unproven():
	source = "__all__ = ['helper']\nclass Key:\n    def __hash__(self): return 0\n    def __eq__(self, other):\n        __all__.append('File')\n        return True\nkey = Key()\nmapping = {key: 0}\nmapping[key]"
	namespace = {}
	exec(compile(source, "<unproven-application-key>", "exec"), namespace)
	assert namespace["__all__"] == ["helper"]
	with pytest.raises(AmbiguousBinding):
		static_exports(ast.parse(source).body)


def test_unknown_short_circuit_joins_states_instead_of_requiring_the_later_operand():
	assert_exports(
		"alias = __all__\nholder = [0]\ndef replace():\n    global alias\n    alias = []\n"
		"holder or replace()\nalias.append('File')",
		changed=True,
	)


def test_conditional_expression_truth_effects_precede_the_selected_value():
	assert_exports(
		"alias = []\nclass Guard:\n    def __bool__(self):\n        global alias\n        alias = __all__\n"
		"        return True\nalias.append('File') if Guard() else None",
		changed=True,
	)


def test_callable_is_captured_before_its_argument_rebinds_the_name():
	assert_exports(
		"def mutate(value):\n    __all__.append('File')\ndef clean(value): pass\n"
		"def swap():\n    global mutate\n    mutate = clean\n    return None\nmutate(swap())",
		changed=True,
	)


def test_setdefault_with_a_literal_none_key_keeps_the_existing_iterator():
	assert_exports(
		"def configure():\n    __all__.append('File')\n    yield 0\n"
		"box = {None: iter(())}\npending = box.setdefault(None, configure())\nnext(pending, None)",
		changed=False,
	)


def test_native_tuple_keys_remain_provable():
	assert_exports("key = (1, 2)\nbox = {key: 0}\nvalue = box[(1, 2)]", changed=False)


def test_abstract_scalar_alternatives_are_not_an_exact_tuple_key():
	assert_exports(
		"class Guard:\n    def __bool__(self): return 1 == 1\n"
		"key = 1 if Guard() else 2\nbox = {1: __all__}\nbox.setdefault(key, []).append('File')",
		changed=True,
	)


def test_abstract_scalar_keys_preserve_possible_assignment_targets():
	assert_exports(
		"class Guard:\n    def __bool__(self): return 1 == 1\n"
		"key = 1 if Guard() else 2\nbox = {1: [], 2: []}\nbox[key] = __all__\nbox[1].append('File')",
		changed=True,
	)


def test_pop_removes_the_old_entry_before_setdefault_selects_a_new_default():
	assert_exports(
		"def configure():\n    __all__.append('File')\n    yield 0\n"
		"box = {'pending': iter(())}\nbox.pop('pending')\npending = box.setdefault('pending', configure())\nnext(pending, None)",
		changed=True,
	)


def test_an_unknown_string_value_is_not_an_exact_empty_key():
	assert_exports("key = str(1)\nbox = {'1': __all__}\nbox.setdefault(key, []).append('File')", changed=True)


def test_setdefault_selects_the_existing_value_from_native_dictionary_pairs():
	assert_exports(
		"def configure():\n    __all__.append('File')\n    yield 0\n"
		"box = dict([('pending', configure())])\npending = box.setdefault('pending', iter(()))\nnext(pending, None)",
		changed=True,
	)


def test_exhaustive_branches_keep_nested_container_aliases():
	assert_exports(
		"enabled = True\nif enabled:\n    box = [__all__]\nelse:\n    box = [__all__]\nbox[0].append('File')",
		changed=True,
	)


def test_augmented_alias_assignment_mutates_the_original_export_list():
	assert_exports("alias = __all__\nalias += ['File']", changed=True)


def test_augmenting_all_does_not_erase_later_mutation_provenance():
	assert_exports("__all__ += []\n__all__.append('File')", changed=True)


def test_conditional_assignment_keeps_the_unmodified_branch_alias():
	assert_exports(
		"enabled = False\nbox = {'value': __all__}\nif enabled:\n    box['value'] = []\nbox['value'].append('File')",
		changed=True,
	)


def test_conditional_update_keeps_the_unmodified_branch_alias():
	assert_exports(
		"box = {'value': __all__}\nif False:\n    box.update(value=[])\nbox['value'].append('File')",
		changed=True,
	)


def test_unconditional_update_can_replace_an_alias_without_mutating_exports():
	assert_exports(
		"box = {'value': __all__}\nbox.update(value=[])\nbox['value'].append('File')", changed=False
	)


def test_kwargs_named_like_locals_are_still_forwarded_to_kwargs():
	assert_exports(
		"def configure(**kwargs):\n    target = None\n    kwargs['target'].append('File')\nconfigure(target=__all__)",
		changed=True,
	)


def test_positional_only_names_can_also_be_kwargs_keys():
	assert_exports(
		"def configure(target, /, **kwargs):\n    kwargs['target'].append('File')\nconfigure(None, target=__all__)",
		changed=True,
	)


def test_variable_keyword_expansion_keeps_possible_argument_values():
	assert_exports(
		"key = 'target'\ndef configure(target):\n    target.append('File')\nconfigure(**{key: __all__})",
		changed=True,
	)


def test_eager_comprehensions_consume_their_generator_iterable():
	assert_exports(
		"def configure():\n    __all__.append('File')\n    yield 0\nvalues = [value for value in configure()]",
		changed=True,
	)


def test_consumption_keeps_references_yielded_by_a_generator():
	assert_exports("def refs():\n    yield __all__\nnext(refs()).append('File')", changed=True)


def test_bound_generator_methods_keep_their_receiver_when_consumed():
	assert_exports(
		"class Config:\n    def configure(self, target):\n        target.append('File')\n        yield 0\nlist(Config().configure(__all__))",
		changed=True,
	)


def test_returned_closures_keep_their_captured_export_alias():
	assert_exports(
		"def factory(target):\n    def configure():\n        target.append('File')\n    return configure\nfactory(__all__)()",
		changed=True,
	)


def test_definition_time_defaults_execute_their_called_helper():
	assert_exports(
		"def configure():\n    __all__.append('File')\ndef deferred(value=configure()):\n    pass",
		changed=True,
	)


def test_locally_shadowed_exports_do_not_mutate_module_exports():
	assert_exports(
		"def configure():\n    __all__ = []\n    __all__.append('File')\nconfigure()", changed=False
	)


def test_storing_a_generator_does_not_execute_it():
	assert_exports(
		"def configure():\n    __all__.append('File')\n    yield 0\nbox = dict(pending=configure())",
		changed=False,
	)


def test_storing_a_callable_does_not_execute_its_body():
	assert_exports("def configure():\n    __all__.append('File')\nbox = [configure]", changed=False)


def test_inert_cyclic_containers_do_not_invalidate_exports():
	assert_exports(
		"enabled = True\nleft = {}\nleft['self'] = left\nright = {}\nright['self'] = right\nif enabled:\n    target = left\nelse:\n    target = right",
		changed=False,
	)


def test_typing_dictionary_write_invalidates_the_false_guard():
	operation = "typing.__dict__.update(TYPE_CHECKING=True)"
	tree = ast.parse("import typing\n" + operation + "\nif typing.TYPE_CHECKING:\n    class File: pass")
	assert any(isinstance(statement, ast.If) for statement in runtime_body(tree.body))


def test_copied_false_typing_constant_survives_a_later_module_attribute_write():
	tree = ast.parse(
		"import typing\nfrom typing import TYPE_CHECKING\ntyping.TYPE_CHECKING = True\nif TYPE_CHECKING:\n    class File: pass"
	)
	assert not any(isinstance(node, ast.If) for node in runtime_body(tree.body))


def test_relative_typing_module_is_not_the_standard_type_checking_guard(bench):
	import_line = "from .typing import TYPE_CHECKING"
	guard = "TYPE_CHECKING"
	module(bench, "typing", "TYPE_CHECKING = True")
	module(bench, "controller", f"{import_line}\nclass File: pass\nif {guard}:\n    class File: pass")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


def test_nonlocal_rebinding_keeps_the_outer_export_alias():
	assert_exports(
		"def factory():\n    target = None\n    def configure():\n        nonlocal target\n        target = __all__\n    configure()\n    target.append('File')\nfactory()",
		changed=True,
	)
