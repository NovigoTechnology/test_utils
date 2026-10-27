"""Regressions for the independent review and Copilot review 5672576169."""

import pytest
from test_validate_override_annotations import (
	annotation,
	function_source,
	hooks,
	register_class,
	register_function,
	validate,
	write,
)
from test_validate_override_annotations import bench as bench

from test_utils.utils.override_annotations import ResolutionError, read_hooks
from test_utils.utils.python_source import extract_function


@pytest.mark.parametrize(
	"configuration",
	[
		"def helper(value=MAPPING.update(ENTRY)): pass",
		"def helper(*, value=MAPPING.update(ENTRY)): pass",
		"helper = lambda value=MAPPING.update(ENTRY): value",
		"class Settings:\n    MAPPING.update(ENTRY)",
		"class Settings:\n    alias = MAPPING\n    alias.update(ENTRY)",
		"class Settings:\n    def helper(self, value=MAPPING.update(ENTRY)): pass",
		"def decorator(function):\n    MAPPING.update(ENTRY)\n    return function\n@decorator\ndef helper(): pass",
		"def decorator(function):\n    MAPPING.update(ENTRY)\n    return function\nclass Settings:\n    @decorator\n    def helper(self): pass",
	],
)
def test_definition_time_mutations_cannot_hide_unannotated_overrides(bench, configuration):
	register_function(bench)
	hooks(
		bench,
		"MAPPING = {}\nENTRY = {'erpnext.accounts.utils.get_coa': 'nagus.overrides.get_coa'}\n"
		+ configuration
		+ "\noverride_whitelisted_methods = MAPPING",
	)
	assert validate(bench).errors


def test_deferred_function_and_method_bodies_do_not_mutate_hook_values(bench):
	path = hooks(
		bench,
		"MAPPING = {}\ndef helper():\n    MAPPING.update(dynamic())\n"
		"class Settings:\n    def helper(self):\n        MAPPING.update(dynamic())\n"
		"override_whitelisted_methods = MAPPING",
	)
	assert read_hooks(path)["override_whitelisted_methods"] == {}


@pytest.mark.parametrize(
	"hook",
	["override_whitelisted_methods", "override_doctype_class", "extend_doctype_class", "required_apps"],
)
@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize(
	"statement",
	["match value:\n    case {hook}: pass", "try:\n    dynamic()\nexcept Exception as {hook}: pass"],
)
def test_synthetic_hook_bindings_are_rejected_even_without_an_earlier_value(bench, hook, prior, statement):
	path = write(
		bench / "nagus/nagus/hooks.py",
		(f"{hook} = {{}}\n" if prior else "") + statement.format(hook=hook),
	)
	with pytest.raises(ResolutionError, match="dynamic configuration"):
		read_hooks(path)
	assert validate(bench).errors


@pytest.mark.parametrize("alias", ["ABC", "Protocol", "object"])
def test_app_base_imported_under_a_standard_library_name_is_preserved(bench, alias):
	register_class(
		bench,
		f"from erpnext.accounts.doctype.ledger.ledger import Ledger as {alias}\nclass CustomLedger({alias}):\n"
		+ function_source(
			annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py"),
			name="validate",
			indent="    ",
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


@pytest.mark.parametrize("right_metadata", [False, True])
def test_app_class_named_ABC_keeps_its_actual_inheritance_precedence(bench, right_metadata):
	write(bench / "nvg_base/nvg_base/base.py", "class ABC:\n    def validate(self): pass")
	doc = (
		annotation(
			method="ABC.validate",
			path="nvg_base/base.py",
			repo="https://github.com/NovigoTechnology/nvg_base",
		)
		if right_metadata
		else annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	)
	register_class(
		bench,
		"from nvg_base.base import ABC\nfrom erpnext.accounts.doctype.ledger.ledger import Ledger\n"
		"class CustomLedger(ABC, Ledger):\n" + function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not right_metadata


@pytest.mark.parametrize(
	"rebinding",
	["get_coa = active", "del get_coa", "from .active import get_coa", "if enabled:\n    get_coa = active"],
)
def test_registered_function_rebindings_cannot_reuse_old_metadata(bench, rebinding):
	path = register_function(bench, annotation())
	write(bench / "nagus/nagus/active.py", "def get_coa(): pass")
	path.write_text(path.read_text() + "\ndef active(): pass\n" + rebinding)
	assert validate(bench).errors
	assert extract_function(path.read_text(), "get_coa") is None


@pytest.mark.parametrize(
	"binding", ["validate = replacement", "validate = lambda self: None", "from .active import validate"]
)
def test_class_bindings_shadowing_upstream_methods_are_not_silently_skipped(bench, binding):
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\ndef replacement(self): pass\n"
		"class CustomLedger(Ledger):\n    " + binding,
	)
	assert validate(bench).errors


def test_non_overriding_callable_alias_is_still_an_ignored_helper(bench):
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\ndef replacement(self): pass\n"
		"class CustomLedger(Ledger):\n    helper = replacement\n"
		+ function_source(
			annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py"),
			name="validate",
			indent="    ",
		),
	)
	assert not validate(bench).errors


def test_redeclared_class_data_attributes_do_not_require_method_annotations(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger:\n    is_virtual = False\n    settings = {'key': 'value'}\n    def validate(self): pass",
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		"    is_virtual = True\n    settings = {'key': 'changed'}\n"
		+ function_source(
			annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py"),
			name="validate",
			indent="    ",
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_upstream_property_shadows_the_parent_method_instead_of_falling_through(bench):
	write(bench / "frappe/frappe/model/document.py", "class Document:\n    def validate(self): pass")
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"from frappe.model.document import Document\nclass Ledger(Document):\n"
		"    @property\n    def validate(self): return self._value\n"
		"    @validate.setter\n    def validate(self, value): self._value = value",
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(
			annotation(
				method="Document.validate",
				path="frappe/model/document.py",
				repo="https://github.com/NovigoTechnology/frappe",
			),
			name="validate",
			indent="    ",
		),
	)
	result = validate(bench)
	assert result.errors
	assert result.checked == 0
	assert any("Ledger.validate" in error and "concrete implementation" in error for error in result.errors)


@pytest.mark.parametrize("annotated", [False, True])
def test_dependency_extension_methods_are_checked_as_upstream_overrides(bench, annotated):
	write(
		bench / "nvg_base/nvg_base/hooks.py",
		"extend_doctype_class = {'Ledger': 'nvg_base.mixins.SharedExtension'}",
	)
	write(
		bench / "nvg_base/nvg_base/mixins.py",
		"class SharedExtension:\n    def extra_validate(self): pass",
	)
	doc = annotation(
		method="SharedExtension.extra_validate",
		path="nvg_base/mixins.py",
		repo="https://github.com/NovigoTechnology/nvg_base",
	)
	register_class(
		bench,
		"class CustomLedger:\n"
		+ function_source(doc if annotated else "", name="extra_validate", indent="    "),
		hook="extend_doctype_class",
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not annotated


def test_multiple_dependency_extension_orders_are_reported_as_unresolved(bench):
	for app in ("erpnext", "nvg_base"):
		write(
			bench / app / app / "hooks.py", f"extend_doctype_class = {{'Ledger': '{app}.mixins.Extension'}}"
		)
	register_class(bench, "class CustomLedger: pass", hook="extend_doctype_class")
	assert any("Unresolved extension composition" in error for error in validate(bench).errors)


def test_same_app_extension_order_follows_reversed_registration_order(bench):
	register_class(
		bench,
		"class Shadowed:\n    def validate(self): pass\nclass CustomLedger:\n"
		+ function_source(
			annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py"),
			name="validate",
			indent="    ",
		),
		hook="extend_doctype_class",
	)
	hooks(
		bench,
		"extend_doctype_class = {'Ledger': ['nagus.overrides.Shadowed', 'nagus.overrides.CustomLedger']}",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors
