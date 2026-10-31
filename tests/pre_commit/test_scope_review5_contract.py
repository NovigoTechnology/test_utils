"""Guard-proof regressions found during the independent scope audit."""

import ast

import pytest

from test_utils.utils.python_source import extract_function, runtime_body, static_exports


@pytest.mark.parametrize(
	"operation",
	[
		"typing.__dict__['TYPE_CHECKING'] = True",
		"typing.__dict__.update(TYPE_CHECKING=True)",
		"from dependency import configure\nconfigure(typing)",
		"from dependency import configure\nconfigure({'module': typing})",
	],
)
def test_module_guard_cannot_survive_dictionary_writes_or_opaque_argument_mutation(operation):
	tree = ast.parse("import typing\n" + operation + "\nif typing.TYPE_CHECKING:\n    class File: pass")
	assert any(isinstance(statement, ast.If) for statement in runtime_body(tree.body))


@pytest.mark.parametrize(
	"operation",
	["typing.__dict__['overload'] = identity", "typing.__dict__.update(overload=identity)"],
)
def test_typing_dictionary_rebinding_cannot_hide_the_new_runtime_implementation(operation):
	source = (
		"import typing\ndef identity(fn): return fn\ndef target(): return 'old'\n"
		+ operation
		+ "\n@typing.overload\ndef target(): return 'new'"
	)
	result = extract_function(source, "target")
	assert result is None or "'new'" in result


@pytest.mark.parametrize("callback", ["len", "str"])
def test_known_builtin_callbacks_use_the_same_effect_contract_as_direct_calls(callback):
	source = f"__all__ = ['helper']\nsorted(['b', 'a'], key={callback})"
	assert static_exports(ast.parse(source).body) == {"helper"}
