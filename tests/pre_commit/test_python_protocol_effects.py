"""Protocol effects: invoked callbacks, captured iterators, and inactive paths.

Only bounded, locally owned snippets execute as the runtime oracle.  Imported
application modules are never executed.
"""

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


@pytest.mark.parametrize(
	"operation",
	[
		"class Configure:\n    def __init__(self):\n        __all__.append('File')\nConfigure()",
		"class Base:\n    def __init__(self):\n        __all__.append('File')\nclass Configure(Base): pass\nConfigure()",
		"class Configure:\n    def __new__(cls):\n        __all__.append('File')\n        return object.__new__(cls)\nConfigure()",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\nbool(Configure())",
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\nlist(Configure())",
		"class Configure:\n    def __str__(self):\n        __all__.append('File')\n        return 'configuration'\nstr(Configure())",
		"class Configure:\n    def __repr__(self):\n        __all__.append('File')\n        return 'configuration'\nrepr(Configure())",
		"class Configure:\n    def __len__(self):\n        __all__.append('File')\n        return 0\nlen(Configure())",
		"class Configure:\n    def __init_subclass__(cls):\n        __all__.append('File')\nclass Child(Configure): pass",
		"class Descriptor:\n    def __get__(self, instance, owner):\n        __all__.append('File')\nclass Configure:\n    value = Descriptor()\nConfigure().value",
		"class Descriptor:\n    def __set_name__(self, owner, name):\n        __all__.append('File')\nclass Configure:\n    value = Descriptor()",
		"class Configure:\n    def __getattribute__(self, name):\n        __all__.append('File')\n        return object.__getattribute__(self, name)\n    value = None\nConfigure().value",
		"class Configure:\n    def __add__(self, other):\n        __all__.append('File')\n        return 0\nConfigure() + 1",
		"class Configure:\n    def __getitem__(self, key):\n        __all__.append('File')\n        return None\nConfigure()[0]",
		"class Configure:\n    def __contains__(self, value):\n        __all__.append('File')\n        return True\n0 in Configure()",
		"class Meta(type):\n    def __new__(mcls, name, bases, namespace):\n        __all__.append('File')\n        return type.__new__(mcls, name, bases, namespace)\nclass Configure(metaclass=Meta): pass",
		"class Configure:\n    def __enter__(self):\n        __all__.append('File')\n    def __exit__(self, *args): pass\nwith Configure(): pass",
		"class Provider:\n    def __mro_entries__(self, bases):\n        __all__.append('File')\n        return ()\nprovider = Provider()\nclass Configure(provider): pass",
		"def values():\n    __all__.append('File')\n    yield 0\ndef accept(*args): pass\naccept(*values())",
		"def values():\n    __all__.append('File')\n    yield 0\nbox = [*values()]",
		"def values():\n    __all__.append('File')\n    yield 0\nbox = (*values(),)",
		"def values():\n    __all__.append('File')\n    yield 0\nbox = {*values()}",
		"from contextlib import contextmanager\n@contextmanager\ndef context():\n    __all__.append('File')\n    yield\nwith context(): pass",
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\nif True:\n    instance = Configure()\nelse:\n    instance = Configure()\nlist(instance)",
		"class Configure:\n    def run(self, target):\n        target.append('File')\nif True:\n    run = Configure().run\nelse:\n    run = Configure().run\nrun(__all__)",
		"class Configure:\n    def __mod__(self, other):\n        __all__.append('File')\n        return 0\nConfigure() % 1",
		"class Configure:\n    def __eq__(self, other):\n        __all__.append('File')\n        return False\nConfigure() == 1",
		"class Configure:\n    def __getattr__(self, name):\n        __all__.append('File')\n        return None\nConfigure().missing",
		"class Configure:\n    def __setattr__(self, name, value):\n        __all__.append('File')\ninstance = Configure()\ninstance.value = 0",
		"class Configure:\n    def __delattr__(self, name):\n        __all__.append('File')\ninstance = Configure()\ndel instance.value",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\nwhile Configure(): pass",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\n[0 for value in [1] if Configure()]",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\nvalue = 1 if Configure() else 2",
		"sorted([1], key=lambda value: __all__.append('File') or value)",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nany([Configure()])",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nall([Configure()])",
		"class Configure:\n    def __hash__(self):\n        __all__.append('File')\n        return 1\nset([Configure()])",
		"class Configure:\n    def __hash__(self):\n        __all__.append('File')\n        return 1\ndict([(Configure(), 1)])",
		"class Configure:\n    def __str__(self):\n        __all__.append('File')\n        return 'configuration'\nprint(Configure())",
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\niterator = iter(Configure())",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nbox = {0: Configure()}\nrows = list(box.items())\nbool(rows[0][1])",
		"class Pure:\n    def __bool__(self): return True\nclass Dirty:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nbox = {1: Pure(), 0: Dirty()}\nrows = list(box.values())\nbool(rows[1])",
		"class Iterator:\n    def __iter__(self): return self\n    def __next__(self):\n        __all__.append('File')\n        raise StopIteration\nclass Iterable:\n    def __iter__(self): return Iterator()\nlist(Iterable())",
	],
)
def test_implicit_protocol_callbacks_cannot_certify_stale_exports(operation):
	assert_export_contract(operation, changed=True)


@pytest.mark.parametrize(
	"operation",
	[
		"class Configure: pass\ninstance = Configure()",
		"class Configure:\n    def __init__(self):\n        self.items = []\ninstance = Configure()",
		"class Configure:\n    def __bool__(self):\n        return True\nbool(Configure())",
		"class Configure:\n    def __iter__(self):\n        return iter([])\nlist(Configure())",
		"class Configure:\n    def __init_subclass__(cls):\n        pass\nclass Child(Configure): pass",
		"if True:\n    box = [None]\n    box[0] = box\nelse:\n    box = [None]\n    box[0] = box",
	],
)
def test_ordinary_class_protocols_do_not_change_actual_exports(operation):
	assert_export_contract(operation, changed=False)


@pytest.mark.parametrize("call", ["configure()", "sorted([0], key=configure)"])
def test_opaque_imported_callback_cannot_prove_unchanged_exports(monkeypatch, call):
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


@pytest.mark.parametrize(
	"operation",
	[
		"class Configure:\n    @property\n    def value(self):\n        __all__.append('File')\n        yield 0\npending = Configure().value",
		"class Descriptor:\n    def __get__(self, instance, owner):\n        __all__.append('File')\n        yield 0\nclass Configure:\n    value = Descriptor()\npending = Configure().value",
	],
)
def test_unconsumed_protocol_generator_has_no_export_effects(operation):
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	exec(compile(source, "<deferred-protocol-oracle>", "exec"), namespace)
	assert namespace["__all__"] == ["helper"]
	assert static_exports(ast.parse(source).body) == {"helper"}


def test_generator_creation_acquires_the_outer_iterator_immediately():
	assert_export_contract(
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\npending = (item for item in Configure())",
		changed=True,
	)


@pytest.mark.parametrize(
	"operation",
	[
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        yield 0\npending = (item for item in Configure())",
		"pending = (__all__.append('File') for item in [0])",
	],
)
def test_generator_creation_does_not_advance_a_generator_body(operation):
	assert_export_contract(operation, changed=False)


@pytest.mark.parametrize("consume", [False, True])
def test_generator_outer_iterator_is_captured_without_reacquiring_it(consume):
	operation = (
		"class Iterator:\n    def __iter__(self):\n        __all__.append('File')\n        return self\n    def __next__(self): raise StopIteration\nclass Configure:\n    def __iter__(self): return Iterator()\npending = (item for item in Configure())"
		+ ("\nlist(pending)" if consume else "")
	)
	assert_export_contract(operation, changed=False)


@pytest.mark.parametrize(
	"operator,reflected,opposite",
	[
		("<", "__gt__", "__lt__"),
		("<=", "__ge__", "__le__"),
		(">", "__lt__", "__gt__"),
		(">=", "__le__", "__ge__"),
	],
)
@pytest.mark.parametrize("mutating", [False, True])
def test_reflected_ordering_uses_only_the_opposite_right_hand_protocol(
	operator, reflected, opposite, mutating
):
	selected = "__all__.append('File')\n        " if mutating else ""
	inactive = "" if mutating else "__all__.append('File')\n        "
	operation = f"class Guard:\n    def {reflected}(self, other):\n        {selected}return False\n    def {opposite}(self, other):\n        {inactive}return False\n0 {operator} Guard()"
	assert_export_contract(operation, changed=mutating)


def test_callable_sentinel_iterator_creation_remains_lazy():
	assert_export_contract(
		"def configure():\n    __all__.append('File')\n    return None\npending = iter(configure, None)",
		changed=False,
	)


@pytest.mark.parametrize("advance", ["list(pending)", "next(pending, None)", "tuple(pending)"])
def test_callable_sentinel_iterator_invokes_callback_only_on_advancement(advance):
	assert_export_contract(
		"def configure():\n    __all__.append('File')\n    return None\npending = iter(configure, None)\n"
		+ advance,
		changed=True,
	)


@pytest.mark.parametrize("protocol,returned", [("__bool__", "False"), ("__len__", "0")])
@pytest.mark.parametrize("consume", [False, True])
def test_generator_filters_truth_test_only_when_consumed(protocol, returned, consume):
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


@pytest.mark.parametrize("phase", ["enter", "exit"])
def test_async_context_manager_awaits_the_selected_protocol(phase):
	enter = "__all__.append('File')\n        " if phase == "enter" else ""
	exit_body = "__all__.append('File')\n        " if phase == "exit" else ""
	operation = f"import asyncio\nclass Configure:\n    async def __aenter__(self):\n        {enter}return self\n    async def __aexit__(self, *args):\n        {exit_body}return None\nasync def main():\n    async with Configure():\n        pass\nasyncio.run(main())"
	assert_export_contract(operation, changed=True)


def test_async_context_manager_does_not_invoke_sync_counterparts():
	assert_export_contract(
		"import asyncio\nclass Configure:\n    async def __aenter__(self): return self\n    async def __aexit__(self, *args): return None\n    def __enter__(self): __all__.append('File')\n    def __exit__(self, *args): __all__.append('File')\nasync def main():\n    async with Configure():\n        pass\nasyncio.run(main())",
		changed=False,
	)


def test_unexecuted_async_context_coroutine_is_lazy():
	assert_export_contract(
		"class Configure:\n    async def __aenter__(self):\n        __all__.append('File')\n        return self\n    async def __aexit__(self, *args): return None\nasync def main():\n    async with Configure():\n        pass\npending = main()\npending.close()",
		changed=False,
	)


@pytest.mark.parametrize("phase", ["acquire", "advance"])
def test_async_iteration_acquires_then_awaits_advancement(phase):
	acquire = "__all__.append('File')\n        " if phase == "acquire" else ""
	advance = "__all__.append('File')\n        " if phase == "advance" else ""
	operation = f"import asyncio\nclass Configure:\n    def __aiter__(self):\n        {acquire}return self\n    async def __anext__(self):\n        {advance}raise StopAsyncIteration\nasync def main():\n    async for item in Configure():\n        pass\nasyncio.run(main())"
	assert_export_contract(operation, changed=True)


def test_async_iteration_follows_the_iterator_returned_by_aiter():
	assert_export_contract(
		"import asyncio\nclass Iterator:\n    async def __anext__(self):\n        __all__.append('File')\n        raise StopAsyncIteration\nclass Configure:\n    def __aiter__(self): return Iterator()\nasync def main():\n    async for item in Configure():\n        pass\nasyncio.run(main())",
		changed=True,
	)


@pytest.mark.parametrize(
	"iteration",
	[
		"items = [item async for item in Configure()]",
		"async for item in (entry async for entry in Configure()):\n        pass",
	],
)
def test_async_comprehensions_and_generator_expressions_await_advancement(iteration):
	operation = (
		"import asyncio\nclass Configure:\n    def __aiter__(self): return self\n    async def __anext__(self):\n        __all__.append('File')\n        raise StopAsyncIteration\nasync def main():\n    "
		+ iteration
		+ "\nasyncio.run(main())"
	)
	assert_export_contract(operation, changed=True)


def test_async_iteration_does_not_invoke_sync_counterparts():
	assert_export_contract(
		"import asyncio\nclass Configure:\n    def __aiter__(self): return self\n    async def __anext__(self): raise StopAsyncIteration\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\n    def __next__(self):\n        __all__.append('File')\n        raise StopIteration\nasync def main():\n    async for item in Configure():\n        pass\nasyncio.run(main())",
		changed=False,
	)


def test_unexecuted_async_iteration_coroutine_is_lazy():
	assert_export_contract(
		"class Configure:\n    def __aiter__(self):\n        __all__.append('File')\n        return self\n    async def __anext__(self): raise StopAsyncIteration\nasync def main():\n    async for item in Configure():\n        pass\npending = main()\npending.close()",
		changed=False,
	)


@pytest.mark.parametrize("mutating", [False, True])
def test_diamond_mro_prefers_c_own_method_over_b_inherited_a_method(mutating):
	base = "return None" if mutating else "__all__.append('File')"
	override = "__all__.append('File')" if mutating else "return None"
	operation = f"class A:\n    def configure(self): {base}\nclass B(A): pass\nclass C(A):\n    def configure(self): {override}\nclass D(B, C): pass\nD().configure()"
	assert_export_contract(operation, changed=mutating)


def test_diamond_mro_keeps_b_own_method_ahead_of_c_own_method():
	assert_export_contract(
		"class A: pass\nclass B(A):\n    def configure(self): return None\nclass C(A):\n    def configure(self): __all__.append('File')\nclass D(B, C): pass\nD().configure()",
		changed=False,
	)


def test_conditional_base_retains_possible_constructor_effects():
	assert_export_contract(
		"class A:\n    def __init__(self): __all__.append('File')\nclass B:\n    def __init__(self): __all__.append('File')\nif True:\n    Base = A\nelse:\n    Base = B\nclass Configure(Base): pass\nConfigure()",
		changed=True,
	)


def test_conditional_base_retains_possible_iterator_effects():
	assert_export_contract(
		"class A:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\nclass B:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\nif True:\n    Base = A\nelse:\n    Base = B\nclass Configure(Base): pass\nlist(Configure())",
		changed=True,
	)
