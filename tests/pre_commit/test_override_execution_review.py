"""Execution boundaries and provenance regressions for PR #11 review 5718805025."""

import ast

import pytest
from test_validate_override_annotations import bench as bench
from test_validate_override_annotations import register_class, validate
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, extract_function, runtime_body, static_exports


def exports(source):
	return static_exports(ast.parse(source).body)


def test_module_decorator_replacing_a_method_is_not_lost_in_member_resolution(bench):
	module(
		bench,
		"api",
		"def replacement(self): pass\ndef replace(fn):\n    return replacement\nclass File:\n    @replace\n    def validate(self): pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File.validate")


@pytest.mark.parametrize("definition", ["class File: pass", "def File(): pass"])
def test_decorator_reading_metadata_preserves_the_original_definition(bench, definition):
	module(
		bench,
		"api",
		"names = []\ndef publish(value):\n    names.append(value.__name__)\n    return value\n@publish\n"
		+ definition,
	)
	assert resolver(bench).resolve("erpnext.api.File").node.name == "File"


def test_module_identity_decorator_is_preserved_during_member_resolution(bench):
	module(
		bench,
		"api",
		"def identity(fn):\n    return fn\nclass File:\n    @identity\n    def validate(self): pass",
	)
	assert resolver(bench).resolve("erpnext.api.File.validate").node.name == "validate"


def test_decorator_can_return_an_intact_alias_of_its_argument(bench):
	module(
		bench,
		"api",
		"def identity(fn):\n    alias = fn\n    return alias\nclass File:\n    @identity\n    def validate(self): pass",
	)
	assert resolver(bench).resolve("erpnext.api.File.validate").node.name == "validate"


def test_all_exhaustive_branches_can_return_the_same_intact_argument(bench):
	module(
		bench,
		"api",
		"enabled = False\ndef identity(fn):\n    if enabled:\n        alias = fn\n    else:\n        alias = fn\n    return alias\nclass File:\n    @identity\n    def validate(self): pass",
	)
	assert resolver(bench).resolve("erpnext.api.File.validate").node.name == "validate"


@pytest.mark.parametrize("identity", [False, True])
def test_historical_extraction_keeps_module_decorator_context(identity):
	source = (
		"def replacement(self): pass\ndef decorate(fn):\n    return "
		+ ("fn" if identity else "replacement")
		+ "\nclass File:\n    @decorate\n    def validate(self): pass"
	)
	assert (extract_function(source, "File.validate") is not None) is identity


@pytest.mark.parametrize("outside", [False, True])
def test_type_checking_only_method_is_not_a_runtime_override(bench, outside):
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\n"
		+ ("from typing import TYPE_CHECKING\n" if outside else "")
		+ "class CustomLedger(Ledger):\n"
		+ ("" if outside else "    from typing import TYPE_CHECKING\n")
		+ "    if TYPE_CHECKING:\n        def validate(self): pass",
	)
	result = validate(bench)
	assert result.checked == 0
	assert not result.errors


def test_nested_class_skips_the_outer_class_namespace():
	with pytest.raises(AmbiguousBinding):
		exports(
			"__all__ = ['helper']\nclass Outer:\n    __all__ = []\n    class Inner:\n        __all__.append('File')"
		)


@pytest.mark.parametrize(
	"source",
	[
		"async def configure():\n    __all__.append('File')\npending = configure()",
		"def configure():\n    __all__.append('File')\n    yield 1\npending = configure()",
		"pending = (__all__.append('File') for item in [0])",
		"def configure():\n    __all__.append('File')\n    yield 1\npending = iter(configure())",
		"snapshot = list(__all__)",
		"def configure():\n    def inner(value=(yield 1)):\n        pass\n    __all__.append('File')\npending = configure()",
		"async def configure():\n    __all__.append('File')\n    yield 1\npending = configure()\nadvance = pending.__anext__()",
	],
)
def test_creating_a_deferred_object_does_not_execute_its_body(source):
	assert exports("__all__ = ['helper']\n" + source) == {"helper"}


@pytest.mark.parametrize(
	"source",
	[
		"def configure():\n    __all__.append('File')\n    yield 1\nlist(configure())",
		"def configure():\n    __all__.append('File')\n    yield 1\npending = configure()\nnext(pending)",
		"pending = (__all__.append('File') for item in [0])\ntuple(pending)",
		"import asyncio\nasync def configure():\n    __all__.append('File')\nasyncio.run(configure())",
		"def inner():\n    __all__.append('File')\n    yield 1\ndef outer():\n    yield from inner()\nlist(outer())",
		"from collections import deque\npending = (__all__.append('File') for item in [0])\ndeque(pending, maxlen=0)",
		"def configure():\n    __all__.append('File')\n    yield 1\nfor value in configure():\n    pass",
		"import asyncio\nasync def inner():\n    __all__.append('File')\nasync def outer():\n    await inner()\nasyncio.run(outer())",
		"def configure():\n    lazy = lambda: (yield 1)\n    __all__.append('File')\nconfigure()",
		"def configure():\n    __all__.append('File')\n    yield 1\npending = configure()\npending.__next__()",
		"import asyncio\nasync def configure():\n    __all__.append('File')\n    yield 1\npending = configure()\nasyncio.run(pending.__anext__())",
	],
)
def test_consuming_a_deferred_object_analyzes_its_effects(source):
	with pytest.raises(AmbiguousBinding):
		exports("__all__ = ['helper']\n" + source)


@pytest.mark.parametrize("parameter", ["t=typing", "*, t=typing"])
def test_function_defaults_capture_the_module_before_its_name_is_rebound(parameter):
	source = f"import typing\ndef configure({parameter}):\n    t.TYPE_CHECKING = True\ntyping = object\nconfigure()\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    class File: pass"
	assert any(isinstance(node, ast.If) for node in runtime_body(ast.parse(source).body))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unexecuted_deferred_helper_does_not_invalidate_a_typing_guard(asynchronous):
	definition = "async def" if asynchronous else "def"
	body = "    t.TYPE_CHECKING = True" + ("" if asynchronous else "\n    yield 1")
	source = f"import typing\n{definition} configure(t=typing):\n{body}\npending = configure()\nif typing.TYPE_CHECKING:\n    class File: pass"
	assert not any(isinstance(node, ast.If) for node in runtime_body(ast.parse(source).body))


def test_captured_default_keeps_the_old_container_after_rebinding():
	assert exports(
		"__all__ = []\ndef configure(target=__all__):\n    target.append('File')\n__all__ = ['helper']\nconfigure()"
	) == {"helper"}


@pytest.mark.parametrize(
	"body",
	[
		"if enabled:\n        return cls",
		"if enabled:\n        return cls\n    else:\n        return None",
		"target = cls\n    target.validate = replacement\n    return cls",
		"targets = [cls]\n    targets[0].validate = replacement\n    return cls",
	],
)
def test_a_decorator_must_return_an_unchanged_argument_on_every_path(bench, body):
	module(
		bench,
		"api",
		f"enabled = False\ndef replacement(self): pass\ndef decorate(cls):\n    {body}\n@decorate\nclass File:\n    def validate(self): pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File.validate")


@pytest.mark.parametrize("condition", ["True", "False"])
def test_conditional_rebinding_preserves_the_old_possible_container(condition):
	with pytest.raises(AmbiguousBinding):
		exports(
			f"__all__ = ['helper']\nalias = __all__\nif {condition}:\n    alias = []\nalias.append('File')"
		)


def test_conditional_alias_rebinding_without_mutation_preserves_exports():
	assert exports("__all__ = ['helper']\nalias = __all__\nif enabled:\n    alias = []") == {"helper"}


def test_generator_creation_evaluates_the_outermost_iterable():
	with pytest.raises(AmbiguousBinding):
		exports(
			"__all__ = ['helper']\ndef source():\n    __all__.append('File')\n    return [0]\npending = (item for item in source())"
		)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
	"operation",
	[
		"alias = __all__\nif enabled:\n    alias = []\nalias.append('File')",
		"def configure(target=__all__):\n    target.append('File')\n__all__ = ['helper']\nconfigure()",
		"def configure():\n    __all__.append('File')\n    yield 1\npending = configure()\nif enabled:\n    list(pending)",
		"pending = (__all__.append('File') for item in [0])\nif enabled:\n    next(pending)",
		"class Outer:\n    __all__ = []\n    class Inner:\n        __all__.append('File')",
	],
)
def test_proven_exports_match_python_across_execution_paths(enabled, operation):
	# Only execute controlled snippets owned by this test, never application code.
	source = f"__all__ = ['helper']\nenabled = {enabled!r}\n" + operation
	namespace = {}
	exec(compile(source, "<execution-provenance-oracle>", "exec"), namespace)
	try:
		proven = exports(source)
	except AmbiguousBinding:
		pass  # Conditional execution may conservatively require manual review.
	else:
		assert proven == set(namespace["__all__"])
