"""Protocol regressions reported in review 5720559061, checked against Python."""

import ast
from contextlib import redirect_stdout
from io import StringIO

import pytest

from test_utils.utils.python_source import AmbiguousBinding, static_exports


def check_exports(operation, *, changed=True):
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	with redirect_stdout(StringIO()):
		exec(compile(source, "<python-protocol-regression>", "exec"), namespace)
	expected = {"helper", "File"} if changed else {"helper"}
	assert set(namespace["__all__"]) == expected
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		assert changed, "Inactive or pure protocols must not invalidate exports"
	else:
		assert proven == expected


@pytest.mark.parametrize("conversion,method", [("r", "__repr__"), ("a", "__repr__"), ("s", "__str__")])
def test_fstring_conversion_dispatches_before_formatting(conversion, method):
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


def test_comparison_result_keeps_the_returned_truth_protocol():
	check_exports(
		"class Result:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nclass Guard:\n    def __lt__(self, other): return Result()\nif Guard() < 1:\n    pass"
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


@pytest.mark.parametrize("operation", ["bool(Guard())", "not Guard()"])
def test_bool_uses_len_only_when_bool_is_absent(operation):
	check_exports(
		"class Guard:\n    def __bool__(self): return True\n    def __len__(self):\n        __all__.append('File')\n        return 0\n"
		+ operation,
		changed=False,
	)


def test_str_falls_back_to_repr():
	check_exports(
		"class Guard:\n    def __repr__(self):\n        __all__.append('File')\n        return 'guard'\nstr(Guard())"
	)


def test_print_does_not_invoke_repr_when_str_is_defined():
	check_exports(
		"class Guard:\n    def __str__(self): return 'guard'\n    def __repr__(self):\n        __all__.append('File')\n        return 'wrong'\nprint(Guard())",
		changed=False,
	)


def test_invalidated_protocol_helper_cannot_reuse_its_old_ast():
	check_exports(
		"def replacement(self):\n    __all__.append('File')\n    return True\nclass Guard:\n    def __bool__(self): return True\nGuard.__bool__.__code__ = replacement.__code__\nbool(Guard())"
	)


@pytest.mark.parametrize(
	"method,setup",
	[
		("__iadd__", "value = Guard()\nvalue += 0"),
		("__add__", "value = Guard()\nvalue += 0"),
		("__radd__", "value = 0\nvalue += Guard()"),
	],
)
def test_augmented_assignment_inspects_inplace_or_fallback(method, setup):
	check_exports(
		f"class Guard:\n    def {method}(self, other):\n        __all__.append('File')\n        return self\n{setup}"
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
