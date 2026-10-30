"""Regressions for PR #11 review 5715468185."""

import ast

import pytest
from test_validate_override_annotations import annotation, function_source, register_class, validate, write
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, runtime_body, static_exports


@pytest.mark.parametrize("correct", [False, True])
def test_imported_upstream_member_preserves_its_defining_symbol(bench, correct):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger:\n    from .api import implementation as validate",
	)
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/api.py", "def implementation(self): pass")
	doc = (
		annotation(method="implementation", path="erpnext/accounts/doctype/ledger/api.py")
		if correct
		else annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not correct


def test_imported_custom_member_reports_the_actual_custom_file_and_symbol(bench):
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n    from .implementation import custom_validate as validate",
	)
	path = write(bench / "nagus/nagus/implementation.py", "def custom_validate(self): pass")
	result = validate(bench)
	assert result.checked == 1
	assert any(
		str(path) in error and "nagus.implementation.custom_validate" in error for error in result.errors
	)


@pytest.mark.parametrize("guard", ["TYPE_CHECKING", "types.TYPE_CHECKING"])
def test_wildcard_after_typing_import_keeps_conditional_runtime_replacements_ambiguous(bench, guard):
	import_line = "from typing import TYPE_CHECKING" if guard == "TYPE_CHECKING" else "import typing as types"
	module(bench, "flags", "TYPE_CHECKING = True\nimport typing as types")
	module(
		bench,
		"controller",
		f"{import_line}\nclass File: pass\nfrom .flags import *\nif {guard}:\n    class File: pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


@pytest.mark.parametrize(
	"import_line,guard",
	[
		("from .typing import TYPE_CHECKING", "TYPE_CHECKING"),
		("from .typing import TYPE_CHECKING as checking", "checking"),
	],
)
def test_relative_typing_module_is_not_the_standard_type_checking_guard(bench, import_line, guard):
	module(bench, "typing", "TYPE_CHECKING = True")
	module(bench, "controller", f"{import_line}\nclass File: pass\nif {guard}:\n    class File: pass")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


def test_absolute_typing_import_after_wildcard_reestablishes_a_false_guard():
	tree = ast.parse(
		"from unknown import *\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    class File: pass"
	)
	assert not any(isinstance(node, ast.If) for node in runtime_body(tree.body))


@pytest.mark.parametrize(
	"import_line,guard",
	[
		("from typing import TYPE_CHECKING as check, Any as check", "check"),
		("import typing as types, other as types", "types.TYPE_CHECKING"),
	],
)
def test_last_alias_in_the_same_import_can_replace_the_typing_guard(import_line, guard):
	tree = ast.parse(f"{import_line}\nif {guard}:\n    class File: pass")
	assert any(isinstance(node, ast.If) for node in runtime_body(tree.body))


@pytest.mark.parametrize(
	"helper",
	[
		"def configure():\n    __all__.append('File')\nconfigure()",
		"def configure():\n    global __all__\n    __all__ = ['File']\nconfigure()",
		"def inner():\n    __all__.append('File')\ndef configure():\n    inner()\nconfigure()",
		"def configure():\n    __all__.append('File')\nrun = configure\nrun()",
	],
)
def test_called_helpers_cannot_hide_mutations_of_export_lists(bench, helper):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", "__all__ = ['helper']\nclass File: pass\n" + helper)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


def test_deferred_uncalled_helpers_do_not_mutate_exports():
	tree = ast.parse("__all__ = ['helper']\ndef configure():\n    __all__.append('File')")
	assert static_exports(tree.body) == {"helper"}


@pytest.mark.parametrize(
	"assignment",
	[
		"__all__, helper = ['File'], None",
		"[__all__, helper] = [['File'], None]",
		"NAMES = ['helper']\nNAMES, = [['File']]\n__all__ = NAMES",
	],
)
def test_unpacking_cannot_leave_stale_static_exports(assignment):
	tree = ast.parse("__all__ = ['helper']\n" + assignment)
	with pytest.raises(AmbiguousBinding):
		static_exports(tree.body)


@pytest.mark.parametrize(
	"helper", ["if enabled:\n    class Helper: pass", "class Helper: pass\nHelper = object"]
)
def test_unrelated_ambiguous_classes_do_not_break_legacy_method_lookup(bench, helper):
	doc = annotation(method="validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger:\n    def validate(self): pass\n" + helper,
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


@pytest.mark.parametrize("correct", [False, True])
def test_inherited_member_uses_the_class_selected_before_later_wildcard_rebinding(bench, correct):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Base:\n    def validate(self): pass\nclass Ledger(Base): pass\nfrom .replacement import *",
	)
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/replacement.py",
		"class Base:\n    def validate(self): return 'replacement'",
	)
	doc = (
		annotation(method="Base.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
		if correct
		else annotation(method="Base.validate", path="erpnext/accounts/doctype/ledger/replacement.py")
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not correct


def test_original_member_survives_a_later_base_without_that_method(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Base:\n    def validate(self): pass\nclass Ledger(Base): pass\nfrom .replacement import *",
	)
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/replacement.py", "class Base: pass")
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(
			annotation(method="Base.validate", path="erpnext/accounts/doctype/ledger/ledger.py"),
			name="validate",
			indent="    ",
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_custom_member_survives_a_later_mixin_rebinding(bench):
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass Mixin:\n"
		+ function_source(doc, name="validate", indent="    ")
		+ "\nclass CustomLedger(Mixin, Ledger): pass\nfrom .replacement import *",
	)
	write(bench / "nagus/nagus/replacement.py", "class Mixin:\n    def validate(self): pass")
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_nested_classes_remain_candidates_for_legacy_method_lookup(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger:\n    class Nested:\n        def validate(self): pass",
	)
	source_resolver = resolver(bench)
	value = source_resolver.function_symbol(
		source_resolver.resolve("erpnext.accounts.doctype.ledger.ledger.Ledger").module,
		"validate",
	)
	assert value.qualname == "Ledger.Nested.validate"
