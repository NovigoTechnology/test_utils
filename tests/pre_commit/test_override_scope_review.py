"""Scope and alias regressions from Copilot and the independent PR #11 review."""

import ast

import pytest
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, runtime_body, static_exports


@pytest.mark.parametrize("target", ["sys.modules[__name__]", "current"])
def test_vars_of_a_module_cannot_preserve_a_stale_controller(bench, target):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(
		bench,
		"api",
		f"import sys\ncurrent = sys.modules[__name__]\nclass File: pass\nvars({target}).update(File=object)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


def test_wildcards_invalidate_an_earlier_proof_that_vars_targets_a_class():
	tree = ast.parse("class Target: pass\nfrom unknown import *\nvars(Target).update(File=object)")
	with pytest.raises(AmbiguousBinding, match="dynamic namespace"):
		static_exports(tree.body, wildcard=lambda statement: False)


@pytest.mark.parametrize(
	"mutation",
	[
		"def configure():\n    import typing as t\n    t.TYPE_CHECKING = True\nconfigure()",
		"def configure():\n    import typing as t\n    alias = t\n    alias.TYPE_CHECKING = True\nconfigure()",
		"setattr(typing, 'TYPE_CHECKING', True)",
		"delattr(typing, 'TYPE_CHECKING')",
		"write = setattr\nwrite(typing, 'TYPE_CHECKING', True)",
		"def configure(t):\n    t.TYPE_CHECKING = True\nconfigure(typing)",
		"aliases = [typing]\naliases[0].TYPE_CHECKING = True",
		"def configure(**kwargs):\n    kwargs['t'].TYPE_CHECKING = True\nconfigure(t=typing)",
		"def configure(t):\n    t.TYPE_CHECKING = True\nconfigure(*[typing])",
	],
)
def test_typing_mutations_through_helper_scopes_and_builtin_aliases_are_not_discarded(mutation):
	tree = ast.parse(f"import typing\n{mutation}\nif typing.TYPE_CHECKING:\n    class File: pass")
	assert any(isinstance(node, ast.If) for node in runtime_body(tree.body))


@pytest.mark.parametrize("body", ["File = object", "del File"])
def test_class_body_globals_affect_the_module_binding(bench, body):
	module(bench, "api", f"class File: pass\nclass Configure:\n    global File\n    {body}")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File")


@pytest.mark.parametrize(
	"helper",
	[
		"def configure():\n    alias = __all__\n    alias.append('File')\nconfigure()",
		"def configure():\n    alias = __all__\n    def inner():\n        alias.append('File')\n    inner()\nconfigure()",
		"class Configure:\n    alias = __all__\n    alias.append('File')",
	],
)
def test_export_mutations_follow_local_aliases_and_closures(helper):
	with pytest.raises(AmbiguousBinding):
		static_exports(ast.parse("__all__ = ['helper']\n" + helper).body)


@pytest.mark.parametrize(
	"helper",
	[
		"def configure():\n    __all__ = []\n    __all__.append('File')\nconfigure()",
		"def configure(__all__):\n    __all__.append('File')\nconfigure([])",
		"class Configure:\n    __all__ = []\n    __all__.append('File')",
	],
)
def test_shadowed_export_containers_are_local(helper):
	assert static_exports(ast.parse("__all__ = ['helper']\n" + helper).body) == {"helper"}


def test_redefined_classes_have_distinct_inheritance_identities(bench):
	module(bench, "api", "class Base: pass\nclass Base(Base): pass\nclass File(Base): pass")
	source = resolver(bench)
	value = source.resolve("erpnext.api.File")
	assert [item.node.lineno for item in source.hierarchy(value)] == [3, 2, 1]


def test_known_local_decorator_cannot_silently_replace_the_selected_class(bench):
	module(
		bench,
		"api",
		"class Replacement: pass\ndef replace(cls):\n    return Replacement\n@replace\nclass File: pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File")


@pytest.mark.parametrize("body", ["cls = Replacement", "cls.validate = lambda self: 'replacement'"])
def test_returning_a_decorator_parameter_is_not_proof_of_an_unchanged_class(bench, body):
	module(
		bench,
		"api",
		f"class Replacement: pass\ndef replace(cls):\n    {body}\n    return cls\n@replace\nclass File: pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File")


def test_alias_of_namespace_builtin_is_analyzed(bench):
	module(bench, "api", "class File: pass\nnamespace = globals\nnamespace().update(File=object)")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File")


@pytest.mark.parametrize(
	"snippet",
	[
		"def configure():\n    alias = __all__\n    alias += ['File']\nconfigure()",
		"def configure():\n    try:\n        alias = __all__\n        alias.append('File')\n    except ValueError:\n        pass\nconfigure()",
		"def configure():\n    return\n    __all__.append('File')\nconfigure()",
		"def configure():\n    local = []\n    local.append('File')\nconfigure()",
		"[__all__.append('File') for __all__ in [[]]]",
		"[alias.append('File') for alias in [__all__]]",
		"class Configure:\n    alias = []\n    alias.append('File')",
		"def configure():\n    for alias in [__all__]:\n        alias.append('File')\nconfigure()",
		"for alias in [__all__]:\n    alias.append('File')",
		"def configure():\n    for __all__ in [[]]:\n        __all__.append('File')\nconfigure()",
		"def configure():\n    if enabled:\n        alias = __all__\n    else:\n        alias = []\n    alias.append('File')\nconfigure()",
	],
)
def test_export_analysis_matches_python_for_owned_scope_examples(snippet):
	# Execute only these controlled snippets, never an application module.
	source = "__all__ = ['helper']\nenabled = True\n" + snippet
	namespace = {}
	exec(compile(source, "<export-scope-oracle>", "exec"), namespace)
	actual = set(namespace["__all__"])
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		assert actual != {"helper"}, "A purely local operation must preserve static exports"
	else:
		assert proven == actual, "Static exports must never preserve a stale list"


def test_c3_linearization_matches_python_for_redefined_classes(bench):
	source = (
		"class Base: marker = 'original'\nclass Old(Base): marker = 'old-child'\n"
		"class Base: marker = 'replacement'\nclass File(Old, Base): marker = 'file'"
	)
	namespace = {}
	exec(compile(source, "<inheritance-oracle>", "exec"), namespace)
	actual = [owner.__dict__["marker"] for owner in namespace["File"].__mro__[:-1]]
	module(bench, "api", source)
	source_resolver = resolver(bench)
	resolved = source_resolver.hierarchy(source_resolver.resolve("erpnext.api.File"))
	assert [ast.literal_eval(owner.node.body[0].value) for owner in resolved] == actual
