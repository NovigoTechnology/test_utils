"""Protocol invocation, short-circuit order, and deferred execution contracts."""

import ast
import sys
from contextlib import redirect_stdout
from io import StringIO
from types import ModuleType

import pytest

from test_utils.utils.python_source import AmbiguousBinding, static_exports


def assert_export_contract(operation, *, changed):
	"""Require a proven result for inactive paths and soundness for mutations."""
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	with redirect_stdout(StringIO()):
		exec(compile(source, "<protocol-effects-oracle>", "exec"), namespace)
	actual = set(namespace["__all__"])
	assert actual == ({"helper", "File"} if changed else {"helper"})
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		assert changed, "An inactive protocol must keep exports provable"
	else:
		assert proven == actual, "Protocol effects must not certify stale exports"


def check_exports(operation, *, changed=True):
	assert_export_contract(operation, changed=changed)


def test_native_dictionary_entries_are_not_protocol_declarations():
	check_exports(
		"def configure(*args):\n    __all__.append('File')\nvalue = {'__bool__': configure}\nbool(value)",
		changed=False,
	)


def test_generator_creation_acquires_the_outer_iterator_immediately():
	assert_export_contract(
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\npending = (item for item in Configure())",
		changed=True,
	)


def test_generator_creation_does_not_advance_a_generator_body():
	operation = "class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        yield 0\npending = (item for item in Configure())"
	assert_export_contract(operation, changed=False)


def test_generator_outer_iterator_is_captured_without_reacquiring_it():
	consume = True
	operation = (
		"class Iterator:\n    def __iter__(self):\n        __all__.append('File')\n        return self\n    def __next__(self): raise StopIteration\nclass Configure:\n    def __iter__(self): return Iterator()\npending = (item for item in Configure())"
		+ ("\nlist(pending)" if consume else "")
	)
	assert_export_contract(operation, changed=False)


@pytest.mark.parametrize("mutating", [False, True])
def test_reflected_ordering_uses_only_the_opposite_right_hand_protocol(mutating):
	operator = "<"
	reflected = "__gt__"
	opposite = "__lt__"
	selected = "__all__.append('File')\n        " if mutating else ""
	inactive = "" if mutating else "__all__.append('File')\n        "
	operation = f"class Guard:\n    def {reflected}(self, other):\n        {selected}return False\n    def {opposite}(self, other):\n        {inactive}return False\n0 {operator} Guard()"
	assert_export_contract(operation, changed=mutating)


def test_callable_sentinel_iterator_creation_remains_lazy():
	assert_export_contract(
		"def configure():\n    __all__.append('File')\n    return None\npending = iter(configure, None)",
		changed=False,
	)


def test_callable_sentinel_iterator_invokes_callback_only_on_advancement():
	advance = "next(pending, None)"
	assert_export_contract(
		"def configure():\n    __all__.append('File')\n    return None\npending = iter(configure, None)\n"
		+ advance,
		changed=True,
	)


@pytest.mark.parametrize("consume", [False, True])
def test_generator_filters_truth_test_only_when_consumed(consume):
	protocol = "__bool__"
	returned = "False"
	operation = (
		f"class Guard:\n    def {protocol}(self):\n        __all__.append('File')\n        return {returned}\npending = (item for item in [0] if Guard())"
		+ ("\nlist(pending)" if consume else "")
	)
	assert_export_contract(operation, changed=consume)


@pytest.mark.parametrize("deleting", [False, True])
@pytest.mark.parametrize("mutating", [False, True])
def test_subscript_writes_select_the_matching_protocol(deleting, mutating):
	setter = "" if deleting == mutating else "__all__.append('File')\n        "
	deleter = "__all__.append('File')\n        " if deleting == mutating else ""
	operation = (
		f"class Configure:\n    def __setitem__(self, key, value):\n        {setter}return None\n    def __delitem__(self, key):\n        {deleter}return None\ninstance = Configure()\n"
		+ ("del instance[0]" if deleting else "instance[0] = 0")
	)
	assert_export_contract(operation, changed=mutating)


def test_async_context_manager_awaits_the_selected_protocol():
	phase = "exit"
	enter = "__all__.append('File')\n        " if phase == "enter" else ""
	exit_body = "__all__.append('File')\n        " if phase == "exit" else ""
	operation = f"import asyncio\nclass Configure:\n    async def __aenter__(self):\n        {enter}return self\n    async def __aexit__(self, *args):\n        {exit_body}return None\nasync def main():\n    async with Configure():\n        pass\nasyncio.run(main())"
	assert_export_contract(operation, changed=True)


def test_async_context_manager_does_not_invoke_sync_counterparts():
	assert_export_contract(
		"import asyncio\nclass Configure:\n    async def __aenter__(self): return self\n    async def __aexit__(self, *args): return None\n    def __enter__(self): __all__.append('File')\n    def __exit__(self, *args): __all__.append('File')\nasync def main():\n    async with Configure():\n        pass\nasyncio.run(main())",
		changed=False,
	)


def test_async_iteration_follows_the_iterator_returned_by_aiter():
	assert_export_contract(
		"import asyncio\nclass Iterator:\n    async def __anext__(self):\n        __all__.append('File')\n        raise StopAsyncIteration\nclass Configure:\n    def __aiter__(self): return Iterator()\nasync def main():\n    async for item in Configure():\n        pass\nasyncio.run(main())",
		changed=True,
	)


def test_async_iteration_does_not_invoke_sync_counterparts():
	assert_export_contract(
		"import asyncio\nclass Configure:\n    def __aiter__(self): return self\n    async def __anext__(self): raise StopAsyncIteration\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\n    def __next__(self):\n        __all__.append('File')\n        raise StopIteration\nasync def main():\n    async for item in Configure():\n        pass\nasyncio.run(main())",
		changed=False,
	)


def test_opaque_imported_callback_cannot_prove_unchanged_exports(monkeypatch):
	call = "configure()"
	namespace = {}
	dependency = ModuleType("_override_audit_dependency")
	dependency.configure = lambda *args: namespace["__all__"].append("File") or 0
	monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
	source = "from _override_audit_dependency import configure\n__all__ = ['helper']\n" + call
	exec(compile(source, "<opaque-callback-oracle>", "exec"), namespace)
	assert set(namespace["__all__"]) == {"helper", "File"}
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		return
	assert proven == set(namespace["__all__"]), (
		"An opaque callback is not evidence that global effects are absent"
	)


def test_fstring_conversion_dispatches_before_formatting():
	conversion = "r"
	method = "__repr__"
	check_exports(
		f"class Guard:\n    def {method}(self):\n        __all__.append('File')\n        return 'guard'\ntext = f'{{Guard()!{conversion}}}'"
	)


def test_fstring_conversion_does_not_format_the_original_object():
	check_exports(
		"class Guard:\n    def __repr__(self): return 'guard'\n    def __format__(self, specification):\n        __all__.append('File')\n        return 'wrong'\ntext = f'{Guard()!r}'",
		changed=False,
	)


def test_chained_comparison_uses_each_preceding_operand():
	check_exports(
		"class Guard:\n    def __gt__(self, other): return True\n    def __lt__(self, other):\n        __all__.append('File')\n        return True\n0 < Guard() < 2"
	)


def test_chained_comparison_truth_tests_intermediate_result():
	check_exports(
		"class Result:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nclass Guard:\n    def __gt__(self, other): return Result()\n    def __lt__(self, other): return True\n0 < Guard() < 2"
	)


def test_binary_result_keeps_the_returned_truth_protocol():
	check_exports(
		"class Result:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nclass Guard:\n    def __add__(self, other): return Result()\nbool(Guard() + 1)"
	)


def test_chained_comparison_stops_on_the_intermediate_truth_result():
	check_exports(
		"class Result:\n    def __bool__(self): return False\nclass Guard:\n    def __gt__(self, other): return Result()\n    def __lt__(self, other):\n        __all__.append('File')\n        return True\n0 < Guard() < 2",
		changed=False,
	)


def test_bool_uses_len_only_when_bool_is_absent():
	operation = "bool(Guard())"
	check_exports(
		"class Guard:\n    def __bool__(self): return True\n    def __len__(self):\n        __all__.append('File')\n        return 0\n"
		+ operation,
		changed=False,
	)


def test_invalidated_protocol_helper_cannot_reuse_its_old_ast():
	check_exports(
		"def replacement(self):\n    __all__.append('File')\n    return True\nclass Guard:\n    def __bool__(self): return True\nGuard.__bool__.__code__ = replacement.__code__\nbool(Guard())"
	)


def test_augmented_assignment_does_not_call_fallback_after_successful_inplace():
	check_exports(
		"class Guard:\n    def __iadd__(self, other): return self\n    def __add__(self, other):\n        __all__.append('File')\n        return self\nvalue = Guard()\nvalue += 0",
		changed=False,
	)


def test_augmented_assignment_uses_fallback_for_notimplemented():
	check_exports(
		"class Guard:\n    def __iadd__(self, other): return NotImplemented\n    def __add__(self, other):\n        __all__.append('File')\n        return self\nvalue = Guard()\nvalue += 0"
	)


def test_binary_subclass_priority_requires_a_different_reflected_implementation():
	check_exports(
		"class Left:\n    def __add__(self, other): return 1\n    def __radd__(self, other):\n        __all__.append('File')\n        return 1\nclass Right(Left): pass\nLeft() + Right()",
		changed=False,
	)


def test_binary_same_class_does_not_dispatch_its_reflected_fallback():
	check_exports(
		"class Guard:\n    def __add__(self, other): return NotImplemented\n    def __radd__(self, other):\n        __all__.append('File')\n        return 1\ntry:\n    Guard() + Guard()\nexcept TypeError:\n    pass",
		changed=False,
	)


def test_exception_type_expression_is_evaluated_after_the_try_body():
	check_exports(
		"try:\n    def configure():\n        __all__.append('File')\n        return ValueError\n    raise ValueError\nexcept configure():\n    pass"
	)


def test_exception_type_expression_is_not_evaluated_without_an_exception():
	check_exports(
		"def configure():\n    __all__.append('File')\n    return ValueError\ntry:\n    pass\nexcept configure():\n    pass",
		changed=False,
	)


def test_unmatched_exception_types_keep_effects_for_later_type_expressions():
	check_exports(
		"alias = []\ndef first():\n    global alias\n    alias = __all__\n    return ValueError\ndef second():\n    alias.append('File')\n    return KeyError\ntry:\n    raise KeyError\nexcept first():\n    pass\nexcept second():\n    pass"
	)


def test_exception_prefix_retains_container_aliases_before_a_later_update():
	check_exports(
		"box = {'value': __all__}\ndef fail(): raise ValueError\ntry:\n    fail()\n    box.update(value=[])\nexcept ValueError:\n    box['value'].append('File')"
	)


def test_constructor_initialization_executes_its_local_callback():
	operation = "class Configure:\n    def __init__(self):\n        __all__.append('File')\nConfigure()"
	assert_export_contract(operation, changed=True)


def test_constructor_allocation_executes_its_local_callback():
	operation = "class Configure:\n    def __new__(cls):\n        __all__.append('File')\n        return object.__new__(cls)\nConfigure()"
	assert_export_contract(operation, changed=True)


def test_descriptor_access_executes_the_getter():
	operation = "class Descriptor:\n    def __get__(self, instance, owner):\n        __all__.append('File')\nclass Configure:\n    value = Descriptor()\nConfigure().value"
	assert_export_contract(operation, changed=True)


def test_class_creation_initializes_named_descriptors():
	operation = "class Descriptor:\n    def __set_name__(self, owner, name):\n        __all__.append('File')\nclass Configure:\n    value = Descriptor()"
	assert_export_contract(operation, changed=True)


def test_subclass_creation_executes_its_parent_hook():
	operation = "class Configure:\n    def __init_subclass__(cls):\n        __all__.append('File')\nclass Child(Configure): pass"
	assert_export_contract(operation, changed=True)


def test_class_creation_executes_its_metaclass_constructor():
	operation = "class Meta(type):\n    def __new__(mcls, name, bases, namespace):\n        __all__.append('File')\n        return type.__new__(mcls, name, bases, namespace)\nclass Configure(metaclass=Meta): pass"
	assert_export_contract(operation, changed=True)


def test_synchronous_context_entry_executes_its_callback():
	operation = "class Configure:\n    def __enter__(self):\n        __all__.append('File')\n    def __exit__(self, *args): pass\nwith Configure(): pass"
	assert_export_contract(operation, changed=True)


def test_hashing_a_set_element_executes_its_hash_protocol():
	operation = "class Configure:\n    def __hash__(self):\n        __all__.append('File')\n        return 1\nset([Configure()])"
	assert_export_contract(operation, changed=True)


def test_sorted_invokes_a_local_lambda_key():
	operation = "sorted([1], key=lambda value: __all__.append('File') or value)"
	assert_export_contract(operation, changed=True)


def test_sorted_invokes_an_opaque_imported_key_callback(monkeypatch):
	call = "sorted([0], key=configure)"
	namespace = {}
	dependency = ModuleType("_override_audit_dependency")
	dependency.configure = lambda *args: namespace["__all__"].append("File") or 0
	monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
	source = "from _override_audit_dependency import configure\n__all__ = ['helper']\n" + call
	exec(compile(source, "<opaque-callback-oracle>", "exec"), namespace)
	assert set(namespace["__all__"]) == {"helper", "File"}
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		return
	assert proven == set(namespace["__all__"]), (
		"An opaque callback is not evidence that global effects are absent"
	)


def test_string_conversion_falls_back_to_repr():
	check_exports(
		"class Guard:\n    def __repr__(self):\n        __all__.append('File')\n        return 'guard'\nstr(Guard())"
	)


def test_string_conversion_does_not_invoke_an_inactive_repr():
	check_exports(
		"class Guard:\n    def __str__(self): return 'guard'\n    def __repr__(self):\n        __all__.append('File')\n        return 'wrong'\nprint(Guard())",
		changed=False,
	)


def test_property_getter_returning_a_generator_keeps_its_body_lazy():
	operation = "class Configure:\n    @property\n    def value(self):\n        __all__.append('File')\n        yield 0\npending = Configure().value"
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	exec(compile(source, "<deferred-protocol-oracle>", "exec"), namespace)
	assert namespace["__all__"] == ["helper"]
	assert static_exports(ast.parse(source).body) == {"helper"}


def test_pure_object_protocols_keep_exports_provable():
	assert_export_contract(
		"class Descriptor:\n    def __get__(self, instance, owner): return 1\n    def __set_name__(self, owner, name): pass\nclass Guard:\n    value = Descriptor()\n    def __init__(self): self.items = []\n    def __init_subclass__(cls): pass\n    def __enter__(self): return self\n    def __exit__(self, *args): return None\n    def __str__(self): return 'guard'\n    def __hash__(self): return 1\nclass Child(Guard): pass\ninstance = Child()\nvalue = instance.value\nwith instance:\n    snapshot = set([instance])\n    text = str(instance)",
		changed=False,
	)


def test_existing_instances_read_replaced_inherited_protocols_from_live_c3():
	assert_export_contract(
		"class Base:\n    def __bool__(self): return True\nclass Child(Base): pass\ninstance = Child()\ndef replacement(self):\n    __all__.append('File')\n    return True\nBase.__bool__ = replacement\nbool(instance)",
		changed=True,
	)


def test_a_child_override_still_shadows_a_replaced_parent_protocol():
	assert_export_contract(
		"class Base:\n    def __bool__(self): return True\nclass Child(Base):\n    def __bool__(self): return True\ndef replacement(self):\n    __all__.append('File')\n    return True\nBase.__bool__ = replacement\nbool(Child())",
		changed=False,
	)


def test_function_decorators_apply_inner_before_outer():
	assert_export_contract(
		"alias = []\ndef outer(function):\n    alias.append('File')\n    return function\ndef inner(function):\n    global alias\n    alias = __all__\n    return function\n@outer\n@inner\ndef target(): pass",
		changed=True,
	)


def test_class_body_runs_before_its_decorator_is_applied():
	assert_export_contract(
		"alias = []\ndef decorate(cls):\n    alias.append('File')\n    return cls\n@decorate\nclass Guard:\n    global alias\n    alias = __all__",
		changed=True,
	)


def test_an_outer_decorator_does_not_retroactively_change_the_inner_argument():
	assert_export_contract(
		"alias = []\ndef outer(function):\n    global alias\n    alias = __all__\n    return function\ndef inner(function):\n    alias.append('File')\n    return function\n@outer\n@inner\ndef target(): pass",
		changed=False,
	)


def test_decorator_expression_is_captured_before_the_class_body_rebinds_it():
	assert_export_contract(
		"def decorate(cls): return cls\ndef replacement(cls):\n    __all__.append('File')\n    return cls\n@decorate\nclass Guard:\n    global decorate\n    decorate = replacement",
		changed=False,
	)


def test_metaclass_preparation_executes_before_the_class_body():
	assert_export_contract(
		"class Meta(type):\n    @classmethod\n    def __prepare__(mcls, name, bases):\n        __all__.append('File')\n        return {}\nclass Guard(metaclass=Meta): pass",
		changed=True,
	)


def test_native_prepared_namespaces_are_available_to_inherited_class_bodies():
	assert_export_contract(
		"class Meta(type):\n    @classmethod\n    def __prepare__(mcls, name, bases): return {'items': []}\nclass Base(metaclass=Meta): pass\nclass Child(Base):\n    items.append('File')",
		changed=False,
	)


def test_a_custom_prepared_mapping_has_an_explicit_uncertainty_boundary():
	source = "__all__ = ['helper']\nclass Namespace(dict): pass\nclass Meta(type):\n    @classmethod\n    def __prepare__(mcls, name, bases): return Namespace()\nclass Guard(metaclass=Meta): pass"
	namespace = {}
	exec(compile(source, "<custom-class-namespace>", "exec"), namespace)
	assert namespace["__all__"] == ["helper"]
	# Custom namespace lookup and write callbacks are outside the native-dict model.
	with pytest.raises(AmbiguousBinding):
		static_exports(ast.parse(source).body)
