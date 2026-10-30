"""Executed export effects reported in PR #11 review 5716798306."""

import ast

import pytest
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, runtime_body, static_exports


@pytest.mark.parametrize(
	"helper",
	[
		"def configure():\n    global File\n    File = Replacement\nconfigure()",
		"def configure():\n    global File\n    del File\nconfigure()",
		"def configure():\n    global File\n    File = Replacement\nrun = configure\nrun()",
	],
)
def test_called_helpers_cannot_leave_stale_public_wildcard_bindings(bench, helper):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", "class File: pass\nclass Replacement: pass\n" + helper)
	with pytest.raises(ResolutionError, match="Ambiguous") as error:
		resolver(bench).resolve("erpnext.controller.File")
	assert "wildcard" in str(error.value)
	assert "File" in str(error.value)


def test_later_explicit_class_replaces_a_helper_global_rebinding(bench):
	module(
		bench,
		"api",
		"def configure():\n    global File\n    File = object\nconfigure()\nclass File: pass",
	)
	assert resolver(bench).resolve("erpnext.api.File").reference == "erpnext.api.File"


def test_helper_local_assignments_do_not_replace_public_bindings(bench):
	module(bench, "api", "class File: pass\ndef configure():\n    File = object\nconfigure()")
	assert resolver(bench).resolve("erpnext.api.File").reference == "erpnext.api.File"


def test_recursive_called_helpers_report_global_rebinding_without_recursing_forever(bench):
	module(
		bench,
		"api",
		"class File: pass\ndef configure():\n    global File\n    File = object\n    configure()\nconfigure()",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File")


@pytest.mark.parametrize(
	"imports,mutation,guard",
	[
		("import typing", "typing.TYPE_CHECKING = True", "typing.TYPE_CHECKING"),
		("import typing", "del typing.TYPE_CHECKING", "typing.TYPE_CHECKING"),
		("import typing as a\nimport typing as b", "a.TYPE_CHECKING = True", "b.TYPE_CHECKING"),
		("import typing\nalias = typing", "alias.TYPE_CHECKING = True", "typing.TYPE_CHECKING"),
		("import typing", "typing.TYPE_CHECKING = True\nimport typing as again", "again.TYPE_CHECKING"),
		("import typing", "typing.TYPE_CHECKING = True\nfrom typing import TYPE_CHECKING", "TYPE_CHECKING"),
		("import typing_extensions as types", "types.TYPE_CHECKING = True", "types.TYPE_CHECKING"),
	],
)
def test_attribute_writes_invalidate_the_shared_typing_module_guard(imports, mutation, guard):
	tree = ast.parse(f"{imports}\n{mutation}\nif {guard}:\n    class File: pass")
	assert any(isinstance(node, ast.If) for node in runtime_body(tree.body))


def test_copied_false_typing_constant_survives_a_later_module_attribute_write():
	tree = ast.parse(
		"import typing\nfrom typing import TYPE_CHECKING\ntyping.TYPE_CHECKING = True\n"
		"if TYPE_CHECKING:\n    class File: pass"
	)
	assert not any(isinstance(node, ast.If) for node in runtime_body(tree.body))


def test_typing_attribute_write_cannot_hide_a_conditional_controller_replacement(bench):
	module(
		bench,
		"controller",
		"import typing as a\nimport typing as b\nclass File: pass\na.TYPE_CHECKING = True\n"
		"if b.TYPE_CHECKING:\n    class File: pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


@pytest.mark.parametrize("definition", ["class File: pass", "def File(): pass"])
def test_bare_decorators_execute_known_helpers_before_export_selection(bench, definition):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(
		bench,
		"api",
		"__all__ = []\ndef publish(value):\n    __all__.append(value.__name__)\n    return value\n"
		+ "@publish\n"
		+ definition,
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


@pytest.mark.parametrize(
	"block",
	["if enabled:\n    from .leaf import *", "try:\n    from .leaf import *\nexcept ImportError:\n    pass"],
)
@pytest.mark.parametrize("alias", [False, True])
def test_nested_wildcards_invalidate_export_lists_and_their_constant_aliases(bench, block, alias):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(
		bench,
		"api",
		("NAMES = ['helper']\n" if alias else "__all__ = ['helper']\n")
		+ block
		+ ("\n__all__ = NAMES" if alias else ""),
	)
	module(bench, "leaf", "__all__ = ['__all__', 'File', 'NAMES']\nNAMES = ['File']\nclass File: pass")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


def test_nested_wildcard_without_exported_all_preserves_a_static_export_filter(bench):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", "__all__ = ['helper']\ndef helper(): pass\nif enabled:\n    from .leaf import *")
	module(bench, "leaf", "class File: pass")
	assert resolver(bench).resolve("erpnext.controller.File").reference == "erpnext.controller.File"


@pytest.mark.parametrize(
	"operation",
	["globals()['File'] = object", "exec('File = object')", "locals().update(File=object)"],
)
def test_namespace_operations_in_called_helpers_cannot_hide_wildcard_exports(bench, operation):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", f"def configure():\n    {operation}\nconfigure()")
	with pytest.raises(ResolutionError, match=r"Ambiguous.*dynamic namespace"):
		resolver(bench).resolve("erpnext.controller.File")


def test_namespace_operations_in_uncalled_helpers_do_not_change_exports(bench):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", "def configure():\n    globals()['File'] = object")
	assert resolver(bench).resolve("erpnext.controller.File").reference == "erpnext.controller.File"


def test_inspecting_a_class_namespace_does_not_replace_the_module_binding(bench):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", "class File: pass\nATTRIBUTES = tuple(vars(File).items())")
	assert resolver(bench).resolve("erpnext.controller.File").reference == "erpnext.api.File"


@pytest.mark.parametrize(
	"wrapper,access",
	[
		("(__all__,)", "WRAPPED[0]"),
		("[(__all__,)]", "WRAPPED[0][0]"),
		("[[[__all__]]]", "WRAPPED[0][0][0]"),
	],
)
def test_mutations_through_nested_containers_invalidate_the_export_list(wrapper, access):
	tree = ast.parse(f"__all__ = ['helper']\nWRAPPED = {wrapper}\n{access}.append('File')")
	with pytest.raises(AmbiguousBinding):
		static_exports(tree.body)


@pytest.mark.parametrize(
	"source",
	[
		"__all__ = ['helper']\n__all__: list[str]",
		"NAMES = ['helper']\nNAMES: list[str]\n__all__ = NAMES",
		"__all__: list[str]\ndef helper(): pass",
	],
)
def test_value_less_annotations_do_not_rebind_export_values(source):
	assert static_exports(ast.parse(source).body) == (None if source.startswith("__all__:") else {"helper"})


def test_annotation_only_all_does_not_block_a_valid_wildcard(bench):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", "__all__ = ['File']\n__all__: list[str]\nclass File: pass")
	assert resolver(bench).resolve("erpnext.controller.File").reference == "erpnext.api.File"
