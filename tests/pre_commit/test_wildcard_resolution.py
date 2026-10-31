"""Ordered static wildcard resolution for override functions and controllers."""

import ast

import pytest
from test_validate_override_annotations import bench as bench
from test_validate_override_annotations import register_class, validate, write

from test_utils.utils.override_annotations import ResolutionError, SourceResolver, locate_app
from test_utils.utils.python_source import extract_function, find_definition


@pytest.fixture
def resolver(bench):
	return SourceResolver([locate_app(bench / name, name) for name in ("nagus", "frappe", "erpnext")])


def test_explicit_class_after_unknown_wildcard_is_resolvable():
	tree = ast.parse("from helpers import *\nclass File:\n    pass\n")
	assert isinstance(find_definition(tree, "File"), ast.ClassDef)


def test_qualified_method_after_unknown_wildcard_can_be_extracted():
	source = "from helpers import *\nclass File:\n    def save_file(self):\n        return 'file'\n"
	assert isinstance(find_definition(ast.parse(source), "File.save_file"), ast.FunctionDef)
	assert "return 'file'" in extract_function(source, "File.save_file")


def test_later_literal_all_excluding_symbol_preserves_definition(bench, resolver):
	write(bench / "nagus/nagus/active.py", "__all__ = ['helper']\ndef helper(): pass\nclass File: pass\n")
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.overrides.File"


def test_literal_all_exclusion_ignores_unrelated_dynamic_values(bench, resolver):
	write(
		bench / "nagus/nagus/active.py",
		"__all__ = ['helper']\nOTHER = object()\ndef helper(): pass\nclass File: pass\n",
	)
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.overrides.File"


def test_later_literal_all_including_symbol_resolves_replacement(bench, resolver):
	write(
		bench / "nagus/nagus/active.py", "__all__ = ('File',)\nclass File:\n    def save_file(self): pass\n"
	)
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.active.File"
	assert resolver.resolve("nagus.overrides.File.save_file").reference == "nagus.active.File.save_file"


def test_later_public_exports_without_symbol_preserve_definition(bench, resolver):
	write(bench / "nagus/nagus/active.py", "class _File: pass\ndef helper(): pass\n")
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.overrides.File"


def test_later_public_export_resolves_replacement_without_all(bench, resolver):
	write(bench / "nagus/nagus/active.py", "class File: pass\n")
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.active.File"


def test_type_checking_import_does_not_become_a_runtime_export(bench, resolver):
	write(bench / "nagus/nagus/other.py", "class File: pass\n")
	write(
		bench / "nagus/nagus/active.py",
		"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .other import File\n",
	)
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.overrides.File"


def test_later_wildcard_follows_relative_explicit_reexport(bench, resolver):
	write(bench / "nagus/nagus/active.py", "class ActualFile: pass\n")
	write(bench / "nagus/nagus/bridge.py", "from .active import ActualFile as File\n__all__ = ['File']\n")
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .bridge import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.active.ActualFile"


def test_later_wildcard_follows_wildcard_reexport(bench, resolver):
	write(bench / "nagus/nagus/active.py", "class File: pass\n")
	write(bench / "nagus/nagus/bridge.py", "from .active import *\n")
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .bridge import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.active.File"


@pytest.mark.parametrize("prior_all", ["", "__all__ = ['File']\n"])
def test_wildcard_reexported_all_is_ambiguous_even_after_a_local_literal(bench, resolver, prior_all):
	write(bench / "nagus/nagus/active.py", "__all__ = ['__all__', '_File']\nclass _File: pass\n")
	write(
		bench / "nagus/nagus/bridge.py",
		prior_all + "class File: pass\nfrom .active import *\n",
	)
	path = write(
		bench / "nagus/nagus/overrides.py", "class File: pass\nclass _File: pass\nfrom .bridge import *\n"
	)
	with pytest.raises(ResolutionError) as caught:
		resolver.resolve("nagus.overrides.File")
	message = str(caught.value)
	assert "ambiguous" in message.lower()
	assert "__all__" in message
	assert f"{path}:3" in message
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("nagus.overrides._File")


def test_dynamic_exports_report_ambiguous_import_location(bench, resolver):
	write(bench / "nagus/nagus/active.py", "__all__ = exported_names()\nclass File: pass\n")
	path = write(bench / "nagus/nagus/overrides.py", "class File:\n    pass\nfrom .active import *\n")
	with pytest.raises(ResolutionError) as caught:
		resolver.resolve("nagus.overrides.File")
	message = str(caught.value)
	assert "ambiguous" in message.lower()
	assert f"{path}:3" in message
	assert "renamed" not in message


def test_export_cycle_without_established_definition_is_ambiguous(bench, resolver):
	write(bench / "nagus/nagus/active.py", "from .bridge import *\n")
	write(bench / "nagus/nagus/bridge.py", "from .active import *\n")
	path = write(bench / "nagus/nagus/overrides.py", "class File:\n    pass\nfrom .active import *\n")
	with pytest.raises(ResolutionError) as caught:
		resolver.resolve("nagus.overrides.File")
	message = str(caught.value)
	assert "ambiguous" in message.lower()
	assert f"{path}:3" in message
	assert "renamed" not in message


def test_file_controller_resolves_its_established_circular_reexport(bench, resolver):
	write(
		bench / "frappe/frappe/core/doctype/file/file.py",
		"""
		from .utils import *
		class File:
		    def save_file(self): pass
		from frappe.core.api.file import *
		""",
	)
	write(bench / "frappe/frappe/core/doctype/file/utils.py", "def helper(): pass\n")
	write(bench / "frappe/frappe/core/api/file.py", "from frappe.core.doctype.file.file import File\n")
	controller = resolver.controller("File", "nagus")
	assert controller.reference == "frappe.core.doctype.file.file.File"
	assert (
		resolver.resolve(f"{controller.reference}.save_file").reference == f"{controller.reference}.save_file"
	)


def test_conditional_later_wildcard_remains_ambiguous(bench, resolver):
	write(bench / "nagus/nagus/active.py", "class File: pass\n")
	path = write(
		bench / "nagus/nagus/overrides.py",
		"class File:\n    pass\nif enabled:\n    from .active import *\n",
	)
	with pytest.raises(ResolutionError) as caught:
		resolver.resolve("nagus.overrides.File")
	assert "ambiguous" in str(caught.value).lower()
	assert f"{path}:4" in str(caught.value)


def test_overload_only_stub_does_not_restore_an_earlier_binding(bench, resolver):
	source = "from typing import overload\nfrom .active import *\n@overload\ndef get_coa(value: str): ...\n"
	prior_definition = source.replace("from .active import *", "def get_coa(): return 'old'")
	assert find_definition(ast.parse(prior_definition), "get_coa") is None
	assert extract_function(prior_definition, "get_coa") is None
	assert find_definition(ast.parse(source), "get_coa") is None
	assert extract_function(source, "get_coa") is None
	write(bench / "nagus/nagus/active.py", "def get_coa(): return 'old'\n")
	write(bench / "nagus/nagus/overrides.py", source)
	with pytest.raises(ResolutionError):
		resolver.resolve("nagus.overrides.get_coa")


def test_missing_symbol_remains_distinct_from_ambiguous_resolution(bench, resolver):
	write(bench / "nagus/nagus/overrides.py", "class File: pass\n")
	with pytest.raises(ResolutionError) as caught:
		resolver.resolve("nagus.overrides.Missing")
	assert "not found" in str(caught.value)
	assert "ambiguous" not in str(caught.value).lower()


def test_extension_validation_reaches_methods_without_executing_modules(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"""
		from .helpers import *
		class Ledger:
		    def validate(self): pass
		    def reconcile(self): pass
		from .api import *
		raise AssertionError('application code must not execute')
		""",
	)
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/helpers.py", "def helper(): pass\n")
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/api.py", "from .ledger import Ledger\n")
	register_class(
		bench,
		"""
		class CustomLedger:
		    def validate(self): pass
		    def reconcile(self): pass
		""",
		hook="extend_doctype_class",
	)
	result = validate(bench)
	assert result.checked == 2
	assert len(result.errors) == 2
	assert all("Missing annotation fields" in error for error in result.errors)
	assert any("Ledger.validate" in error for error in result.errors)
	assert any("Ledger.reconcile" in error for error in result.errors)
