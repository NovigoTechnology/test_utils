"""Wildcard/reexport selection and validator diagnostics without importing apps."""

import ast

import pytest
from test_validate_override_annotations import annotation, function_source, register_class, validate, write
from test_validate_override_annotations import bench as bench

from test_utils.utils.override_annotations import ResolutionError, SourceResolver, locate_app
from test_utils.utils.python_source import extract_function, find_definition


def resolver(bench):
	return SourceResolver(
		[locate_app(bench / name, name) for name in ("nagus", "frappe", "erpnext", "nvg_base")]
	)


def module(bench, name, source):
	return write(bench / "erpnext/erpnext" / (name.replace(".", "/") + ".py"), source)


def test_explicit_class_and_qualified_method_after_unknown_wildcard_are_proven():
	source = "from missing import *\nclass File:\n    def save_file(self): pass\n"
	assert isinstance(find_definition(ast.parse(source), "File"), ast.ClassDef)
	assert extract_function(source, "File.save_file") == "    def save_file(self): pass"


@pytest.mark.parametrize("exports", ["__all__ = ['helper']\nclass File: pass", "def helper(): pass"])
def test_later_wildcard_excluding_the_symbol_preserves_the_previous_class(bench, exports):
	module(bench, "controller", "class File:\n    def save_file(self): pass\nfrom .api import *")
	module(bench, "api", exports)
	value = resolver(bench).resolve("erpnext.controller.File.save_file")
	assert value.reference == "erpnext.controller.File.save_file"


@pytest.mark.parametrize("explicit_all", [False, True])
def test_later_wildcard_resolves_the_replacement_class_and_its_actual_method(bench, explicit_all):
	module(bench, "controller", "class File:\n    def save_file(self): pass\nfrom .api import *")
	module(
		bench,
		"api",
		("__all__ = ['File']\n" if explicit_all else "")
		+ "class File:\n    def save_file(self): return 'replacement'",
	)
	value = resolver(bench).resolve("erpnext.controller.File.save_file")
	assert value.reference == "erpnext.api.File.save_file"


def test_reexported_class_keeps_its_defining_module(bench):
	module(bench, "controller", "from .bridge import *")
	module(bench, "bridge", "__all__ = ['File']\nfrom .api import File")
	module(bench, "api", "class File:\n    def save_file(self): pass")
	value = resolver(bench).resolve("erpnext.controller.File.save_file")
	assert value.reference == "erpnext.api.File.save_file"


def test_public_wildcard_reexport_chain_resolves_without_explicit_all(bench):
	module(bench, "controller", "from .bridge import *")
	module(bench, "bridge", "from .api import *")
	module(bench, "api", "class File: pass")
	assert resolver(bench).resolve("erpnext.controller.File").reference == "erpnext.api.File"


@pytest.mark.parametrize(
	"exports",
	["__all__ = ['helper']\nfrom .leaf import *", "NAMES = ['helper']\nfrom .leaf import *\n__all__ = NAMES"],
)
def test_wildcards_cannot_silently_rebind_all_or_its_constant_aliases(bench, exports):
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", exports)
	module(bench, "leaf", "__all__ = ['__all__', 'NAMES', 'File']\nNAMES = ['File']\nclass File: pass")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.controller.File")


def test_file_style_circular_reexport_uses_the_already_initialized_class(bench):
	module(
		bench,
		"controller",
		"from unknown_helpers import *\nclass File:\n    def save_file(self): pass\nfrom .api import *",
	)
	module(bench, "api", "from .controller import File\ndef helper(): pass")
	value = resolver(bench).resolve("erpnext.controller.File.save_file")
	assert value.reference == "erpnext.controller.File.save_file"


@pytest.mark.parametrize(
	"exports",
	[
		"__all__ = ['helper']\nALIAS = __all__\nALIAS.append('File')",
		"def __getattr__(name): return dynamic_exports()",
	],
)
def test_dynamic_exports_report_ambiguity_and_the_responsible_import(bench, exports):
	path = module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", exports)
	with pytest.raises(ResolutionError, match="Ambiguous") as error:
		resolver(bench).resolve("erpnext.controller.File")
	assert "wildcard" in str(error.value)
	assert f"{path}:2" in str(error.value)
	assert "erpnext.api" in str(error.value)


def test_unresolvable_cycles_are_explicit_instead_of_missing_controller_errors(bench):
	module(bench, "controller", "from .api import *")
	module(bench, "api", "from .controller import *")
	with pytest.raises(ResolutionError, match=r"Ambiguous.*cyclic"):
		resolver(bench).resolve("erpnext.controller.File")


@pytest.mark.parametrize("annotated", [False, True])
def test_controller_with_file_style_reexports_reaches_mixin_annotation_checks(bench, annotated):
	path = bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py"
	write(
		path,
		"from frappe.model.document import Document\nclass Ledger(Document):\n    def validate(self): pass\nfrom .api import *",
	)
	write(path.with_name("api.py"), "from .ledger import Ledger")
	register_class(
		bench,
		"class CustomLedger:\n"
		+ function_source(
			annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
			if annotated
			else "",
			name="validate",
			indent="    ",
		),
		hook="extend_doctype_class",
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not annotated
	if not annotated:
		assert any("Missing annotation fields" in error for error in result.errors)
	assert not any("Controller" in error and "not found" in error for error in result.errors)


def test_inspecting_exports_never_executes_application_code(bench, tmp_path):
	marker = tmp_path / "must-not-exist"
	module(bench, "controller", "from .api import *")
	module(bench, "api", f"open({str(marker)!r}, 'w').write('executed')\nclass File: pass")
	assert resolver(bench).resolve("erpnext.controller.File").reference == "erpnext.api.File"
	assert not marker.exists()


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


def test_called_helpers_cannot_leave_stale_public_wildcard_bindings(bench):
	helper = "def configure():\n    global File\n    File = Replacement\nconfigure()"
	module(bench, "controller", "class File: pass\nfrom .api import *")
	module(bench, "api", "class File: pass\nclass Replacement: pass\n" + helper)
	with pytest.raises(ResolutionError, match="Ambiguous") as error:
		resolver(bench).resolve("erpnext.controller.File")
	assert "wildcard" in str(error.value)
	assert "File" in str(error.value)


def test_helper_local_assignments_do_not_replace_public_bindings(bench):
	module(bench, "api", "class File: pass\ndef configure():\n    File = object\nconfigure()")
	assert resolver(bench).resolve("erpnext.api.File").reference == "erpnext.api.File"
