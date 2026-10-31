"""Conditional updates cannot erase aliases reachable on another branch."""

import ast

import pytest

from test_utils.utils.python_source import AmbiguousBinding, static_exports


@pytest.mark.parametrize("update", ["box.update(value=[])", "box.update({'value': []})"])
def test_conditional_update_retains_the_preexisting_container_alias(update):
	source = (
		"__all__ = ['helper']\nbox = {'value': __all__}\nif False:\n    "
		+ update
		+ "\nbox['value'].append('File')"
	)
	namespace = {}
	exec(compile(source, "<conditional-update>", "exec"), namespace)
	assert namespace["__all__"] == ["helper", "File"]
	with pytest.raises(AmbiguousBinding):
		static_exports(ast.parse(source).body)


def test_unconditional_update_can_replace_an_alias_without_mutating_the_export_list():
	source = (
		"__all__ = ['helper']\nbox = {'value': __all__}\nbox.update(value=[])\nbox['value'].append('File')"
	)
	assert static_exports(ast.parse(source).body) == {"helper"}
