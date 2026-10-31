"""Bounded execution-oracle regressions for the reported protocol families."""

import ast

import pytest

from test_utils.utils.python_source import AmbiguousBinding, static_exports


def assert_export_contract(operation, *, changed):
	"""Execute controlled test snippets only, never application modules."""
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	exec(compile(source, "<review6-protocol-oracle>", "exec"), namespace)
	actual = set(namespace["__all__"])
	assert actual == ({"helper", "File"} if changed else {"helper"})
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		assert changed, "A proven inactive protocol must not make exports ambiguous"
	else:
		assert proven == actual, "Protocol effects must not certify stale exports"


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
