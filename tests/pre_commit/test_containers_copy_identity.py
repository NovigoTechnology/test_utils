"""Sequence concatenation copies its outer container and shares nested objects."""

import ast

import pytest
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, static_exports


def execute(source):
	namespace = {}
	exec(compile(source, "<container-copy-identity>", "exec"), namespace)
	return namespace


def test_concatenating_the_same_list_twice_creates_a_separate_export_list():
	source = "NAMES = ['helper']\n__all__ = NAMES + NAMES\n__all__.append('File')"
	namespace = execute(source)
	assert namespace["NAMES"] == ["helper"]
	assert namespace["__all__"] == ["helper", "helper", "File"]
	with pytest.raises(AmbiguousBinding):
		static_exports(ast.parse(source).body)


def test_mutating_a_concatenated_copy_does_not_mutate_the_export_operand():
	source = "__all__ = ['helper']\ncopy = __all__ + __all__\ncopy.append('File')"
	assert execute(source)["__all__"] == ["helper"]
	assert static_exports(ast.parse(source).body) == {"helper"}


def test_concatenated_containers_keep_the_same_nested_mutable_values():
	source = "__all__ = ['helper']\nbox = [__all__]\ncopy = box + box\ncopy[1].append('File')"
	namespace = execute(source)
	assert namespace["copy"][0] is namespace["copy"][1] is namespace["__all__"]
	assert namespace["__all__"] == ["helper", "File"]
	with pytest.raises(AmbiguousBinding):
		static_exports(ast.parse(source).body)


def test_rebinding_a_tuple_operand_does_not_change_its_concatenated_copy():
	source = "NAMES = ('helper',)\n__all__ = NAMES + NAMES\nNAMES += ('File',)"
	namespace = execute(source)
	assert namespace["NAMES"] == ("helper", "File")
	assert namespace["__all__"] == ("helper", "helper")
	assert static_exports(ast.parse(source).body) == {"helper"}


def test_rebinding_a_concatenated_tuple_keeps_the_old_captured_default(bench):
	source = "names = ([],)\ncopy = names + names\ndef target(option=copy): pass\ncopy += (3,)"
	namespace = execute(source)
	assert len(namespace["copy"]) == 3
	assert len(namespace["target"].__defaults__[0]) == 2
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.name == "target"


def test_clearing_a_keyword_defaults_copy_does_not_mutate_the_imported_function(bench):
	module(bench, "api", "def target(*, option=1): pass")
	module(bench, "bridge", "from .api import target\ncopy = target.__kwdefaults__.copy()\ncopy.clear()")
	assert resolver(bench).resolve("erpnext.bridge.target").reference == "erpnext.api.target"


def test_keyword_defaults_copy_keeps_the_shared_mutable_default(bench):
	module(bench, "api", "def target(*, option=[]): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ncopy = target.__kwdefaults__.copy()\ncopy['option'].append(2)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")
