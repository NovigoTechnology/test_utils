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
