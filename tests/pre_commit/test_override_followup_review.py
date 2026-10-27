"""Regressions for PR #6 review 5672352839."""

import pytest
from test_validate_override_annotations import (
	annotation,
	function_source,
	git,
	hooks,
	register_class,
	register_function,
	validate,
	write,
)
from test_validate_override_annotations import bench as bench

from test_utils.pre_commit.validate_override_annotations import main
from test_utils.utils.override_annotations import ResolutionError, read_hooks


@pytest.mark.parametrize(
	"statement",
	[
		"if condition:\n    TARGET = 'nagus.overrides.unannotated'",
		"if condition:\n    del TARGET",
		"for TARGET in targets:\n    pass",
		"try:\n    TARGET = dynamic()\nexcept Exception:\n    pass",
		"try:\n    dynamic()\nexcept Exception as TARGET:\n    pass",
		"match value:\n    case {'target': TARGET}:\n        pass",
		"if condition:\n    def TARGET(): pass",
		"TARGET, = ('nagus.overrides.unannotated',)",
		"(TARGET := 'nagus.overrides.unannotated')",
		"def configure():\n    global TARGET\n    TARGET = 'nagus.overrides.unannotated'\nconfigure()",
	],
)
def test_uncertain_string_bindings_cannot_reuse_a_stale_override_target(bench, statement):
	register_function(bench, annotation())
	hooks(
		bench,
		"TARGET = 'nagus.overrides.get_coa'\n"
		+ statement
		+ "\noverride_whitelisted_methods = {'erpnext.accounts.utils.get_coa': TARGET}",
	)
	result = validate(bench)
	assert result.errors
	assert result.checked == 0


def test_string_alias_snapshot_survives_later_rebinding(bench):
	register_function(bench, annotation())
	hooks(
		bench,
		"TARGET = 'nagus.overrides.get_coa'\n"
		"override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': TARGET}\n"
		"if condition:\n    TARGET = 'nagus.overrides.other'",
	)
	assert not validate(bench).errors


@pytest.mark.parametrize(
	"statement",
	[
		"from .registry import override_whitelisted_methods",
		"from .registry import other as override_whitelisted_methods",
		"import registry as override_whitelisted_methods",
		"from .registry import required_apps",
		"from .registry import *",
		"if condition:\n    from .registry import override_whitelisted_methods",
	],
)
@pytest.mark.parametrize("prior", ["", "override_whitelisted_methods = {}\nrequired_apps = []\n"])
def test_hook_imports_are_explicitly_rejected(bench, statement, prior):
	path = write(bench / "nagus/nagus/hooks.py", prior + statement)
	with pytest.raises(ResolutionError, match="import"):
		read_hooks(path)
	assert validate(bench).errors


def test_imported_alias_cannot_reuse_an_earlier_static_binding(bench):
	path = write(
		bench / "nagus/nagus/hooks.py",
		"TARGET = 'nagus.overrides.get_coa'\nfrom .registry import TARGET\n"
		"override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': TARGET}",
	)
	with pytest.raises(ResolutionError, match="static"):
		read_hooks(path)


def test_dependency_hook_import_checks_respect_selected_hooks(bench):
	path = write(
		bench / "nvg_base/nvg_base/hooks.py",
		"required_apps = ('erpnext',)\nfrom .registry import override_doctype_class",
	)
	assert read_hooks(path, only={"required_apps"}) == {"required_apps": ("erpnext",)}
	write(path, "from .registry import required_apps")
	with pytest.raises(ResolutionError, match="import"):
		read_hooks(path, only={"required_apps"})


def test_repository_mismatch_never_logs_origin_credentials(bench, capsys):
	register_function(bench, annotation(repo="https://github.com/other/erpnext"))
	git(
		bench / "erpnext",
		"remote",
		"set-url",
		"origin",
		"https://fixture-user:fixture-secret@github.com/NovigoTechnology/erpnext.git",
	)
	assert main(["--app", str(bench / "nagus"), "--apps-root", str(bench)]) == 1
	output = capsys.readouterr()
	assert "REPO does not match" in output.err
	assert "github.com/novigotechnology/erpnext" in output.err
	assert "fixture-secret" not in output.err + output.out
	assert "fixture-user" not in output.err + output.out


@pytest.mark.parametrize("collection", ["()", "[]"])
def test_augmented_dependency_assignment_preserves_python_alias_semantics(bench, collection):
	addition = "('erpnext',)" if collection == "()" else "['erpnext']"
	path = write(
		bench / "nagus/nagus/hooks.py",
		f"BASE = {collection}\nrequired_apps = BASE\nBASE += {addition}",
	)
	required = read_hooks(path)["required_apps"]
	assert required == (() if collection == "()" else ["erpnext"])
	if collection == "()":
		# No erpnext checkout is needed for the tuple snapshot, which remains empty.
		(bench / "erpnext").rename(bench / "unused_erpnext")
	assert not validate(bench).errors


def test_tuple_hook_values_and_transitive_dependencies_are_accepted(bench):
	register_function(bench, annotation())
	hooks(
		bench,
		"required_apps = ('NovigoTechnology/nvg_base',)\n"
		"override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': ('nagus.overrides.get_coa',)}",
	)
	write(bench / "nvg_base/nvg_base/hooks.py", "required_apps = ('erpnext',)")
	assert not validate(bench).errors


@pytest.mark.parametrize(
	"import_line,base",
	[
		("from abc import ABC as AbstractBase", "AbstractBase"),
		("import abc as abstract", "abstract.ABC"),
		("from typing import Protocol as Interface", "Interface"),
		("import typing as types", "types.Protocol"),
		("from typing_extensions import Protocol as Interface", "Interface"),
		("from builtins import object as Root", "Root"),
	],
)
def test_standard_library_base_aliases_do_not_block_override_validation(bench, import_line, base):
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		f"{import_line}\nfrom erpnext.accounts.doctype.ledger.ledger import Ledger\n"
		f"class CustomLedger(Ledger, {base}):\n" + function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


@pytest.mark.parametrize("replacement_app", ["nagus", "nvg_base"])
@pytest.mark.parametrize("right_metadata", [False, True])
def test_extensions_use_the_registered_replacement_controller(bench, replacement_app, right_metadata):
	write(
		bench / "frappe/frappe/model/document.py",
		"class Document:\n    def validate(self): return 'document'",
	)
	write(
		bench / replacement_app / replacement_app / "replacement.py",
		"from frappe.model.document import Document\nclass Replacement(Document): pass",
	)
	replacement = f"override_doctype_class = {{'Ledger': '{replacement_app}.replacement.Replacement'}}\n"
	hooks(
		bench,
		(replacement if replacement_app == "nagus" else "")
		+ "extend_doctype_class = {'Ledger': 'nagus.overrides.Extension'}",
	)
	if replacement_app != "nagus":
		write(bench / replacement_app / replacement_app / "hooks.py", replacement)
	doc = (
		annotation(
			method="Document.validate",
			path="frappe/model/document.py",
			repo="https://github.com/NovigoTechnology/frappe",
		)
		if right_metadata
		else annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	)
	write(
		bench / "nagus/nagus/overrides.py",
		"class Extension:\n" + function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not right_metadata
	if not right_metadata:
		assert any("Document.validate" in error for error in result.errors)


@pytest.mark.parametrize("annotated", [False, True])
def test_helper_property_does_not_abort_other_method_validation(bench, annotated):
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\n"
		"class CustomLedger(Ledger):\n"
		"    @property\n    def helper(self): return self._helper\n"
		"    @helper.setter\n    def helper(self, value): self._helper = value\n"
		+ function_source(doc if annotated else "", name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not annotated
	assert not any("concrete implementation" in error for error in result.errors)


def test_unresolved_overriding_property_does_not_hide_other_missing_annotations(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger:\n    @property\n    def helper(self): return 'upstream'\n    def validate(self): pass",
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		"    @property\n    def helper(self): return self._helper\n"
		"    @helper.setter\n    def helper(self, value): self._helper = value\n"
		"    def validate(self): pass",
	)
	result = validate(bench)
	assert any("concrete implementation" in error and "helper" in error for error in result.errors)
	assert any("Missing annotation fields" in error and "validate" in error for error in result.errors)
	assert result.checked == 1


def test_conflicting_dependency_replacements_report_missing_app_order(bench):
	hooks(bench, "extend_doctype_class = {'Ledger': 'nagus.overrides.Extension'}")
	write(bench / "nagus/nagus/overrides.py", "class Extension: pass")
	for app in ("nvg_base", "erpnext"):
		write(
			bench / app / app / "hooks.py",
			f"override_doctype_class = {{'Ledger': '{app}.replacement.Replacement'}}",
		)
	result = validate(bench)
	assert any("Ambiguous replacement controller" in error for error in result.errors)
