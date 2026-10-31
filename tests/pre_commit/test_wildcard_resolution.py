"""Ordered static wildcard resolution for override functions and controllers."""

import ast

import pytest
from test_validate_override_annotations import annotation, function_source, register_class, validate, write
from test_validate_override_annotations import bench as bench

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
	write(
		bench / "nagus/nagus/active.py",
		"__all__: list[str] = ['helper']\ndef helper(): pass\nclass File: pass\n",
	)
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


def test_redefined_base_classes_keep_both_override_methods(bench):
	write(
		bench / "erpnext/erpnext/bases.py",
		"""
		class Base:
		    def from_first_base(self): pass
		class Left(Base): pass
		class Base:
		    def from_second_base(self): pass
		class Right(Base): pass
		class Controller(Left, Right): pass
		""",
	)
	register_class(
		bench,
		"""
		from erpnext.bases import Controller
		class CustomLedger(Controller):
		    def from_first_base(self): pass
		    def from_second_base(self): pass
		""",
	)
	result = validate(bench)
	assert result.checked == 2
	assert len(result.errors) == 2
	assert all("Missing annotation fields" in error for error in result.errors)
	assert any("erpnext.bases.Base.from_first_base" in error for error in result.errors)
	assert any("erpnext.bases.Base.from_second_base" in error for error in result.errors)


def test_package_reexport_wins_over_a_same_named_child_module(bench, resolver):
	path = write(bench / "nagus/nagus/tasks/run.py", "def run(): pass\n")
	write(bench / "nagus/nagus/tasks/__init__.py", "from .run import run\n")
	write(bench / "nagus/nagus/overrides.py", "from .tasks import *\n")
	write(bench / "nagus/nagus/provider_override.py", "from .tasks.run import *\n")
	package_export = resolver.resolve("nagus.tasks.run")
	assert isinstance(package_export.node, ast.FunctionDef)
	assert package_export.module.path == path
	assert package_export.qualname == "run"
	assert resolver.resolve("nagus.overrides.run").node is package_export.node
	assert resolver.resolve("nagus.provider_override.run").node is package_export.node


def test_all_alias_mutation_and_destructured_assignments_are_ambiguous(bench, resolver):
	write(
		bench / "nagus/nagus/active.py",
		"__all__ = names = ['helper']\nnames.append('File')\ndef helper(): pass\nclass File: pass\n",
	)
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("nagus.overrides.File")
	write(
		bench / "nagus/nagus/destructured.py",
		"__all__, names = ('helper', 'File')\ndef helper(): pass\nclass File: pass\n",
	)
	write(bench / "nagus/nagus/other_overrides.py", "class File: pass\nfrom .destructured import *\n")
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("nagus.other_overrides.File")


def test_known_false_type_checking_skips_an_unavailable_wildcard(bench, resolver):
	write(
		bench / "nagus/nagus/active.py",
		"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from unavailable import *\n",
	)
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.overrides.File"


def test_type_checking_guard_rebinding_and_earlier_wildcard_remain_ambiguous(bench, resolver):
	write(bench / "nagus/nagus/flags.py", "TYPE_CHECKING = True\n")
	write(
		bench / "nagus/nagus/active.py",
		"from typing import TYPE_CHECKING\nfrom .flags import *\nif TYPE_CHECKING:\n    class File: pass\n",
	)
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("nagus.overrides.File")
	write(
		bench / "nagus/nagus/rebound.py",
		"from typing import TYPE_CHECKING\nTYPE_CHECKING = True\nif TYPE_CHECKING:\n    class File: pass\n",
	)
	write(bench / "nagus/nagus/other_overrides.py", "class File: pass\nfrom .rebound import *\n")
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("nagus.other_overrides.File")


def test_explicit_import_after_wildcard_restores_the_type_checking_guard(bench, resolver):
	write(bench / "nagus/nagus/flags.py", "TYPE_CHECKING = True\n")
	write(
		bench / "nagus/nagus/active.py",
		"from .flags import *\nfrom typing import TYPE_CHECKING\n"
		"if TYPE_CHECKING:\n    from unavailable import *\n",
	)
	write(bench / "nagus/nagus/overrides.py", "class File: pass\nfrom .active import *\n")
	assert resolver.resolve("nagus.overrides.File").reference == "nagus.overrides.File"


def test_type_checking_else_bases_respect_the_class_declaration_position(bench, resolver):
	write(
		bench / "nagus/nagus/bases.py",
		"""
		from typing import TYPE_CHECKING
		if TYPE_CHECKING:
		    from unavailable import *
		else:
		    class Base:
		        def original(self): pass
		    class Derived(Base): pass
		    class Base:
		        def replacement(self): pass
		""",
	)
	derived, base = resolver.hierarchy(resolver.resolve("nagus.bases.Derived"))
	assert base.node.lineno < derived.node.lineno
	assert isinstance(resolver.method(base, "original"), ast.FunctionDef)
	assert resolver.method(base, "replacement") is None


@pytest.mark.parametrize("module", ["typing", "typing_extensions"])
def test_guard_mutation_survives_reimport_under_another_alias(bench, resolver, module):
	source = (
		f"import {module} as original\noriginal.TYPE_CHECKING = True\nimport {module} as flags\n"
		"def run(): return 'old'\nif flags.TYPE_CHECKING:\n    def run(): return 'replacement'\n"
	)
	assert find_definition(ast.parse(source), "run") is None
	assert extract_function(source, "run") is None
	write(bench / "nagus/nagus/active.py", source)
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("nagus.active.run")
	direct_source = source.replace(
		f"import {module} as flags", f"from {module} import TYPE_CHECKING as flags"
	).replace("if flags.TYPE_CHECKING:", "if flags:")
	assert find_definition(ast.parse(direct_source), "run") is None
	assert extract_function(direct_source, "run") is None
	write(bench / "nagus/nagus/direct_active.py", direct_source)
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("nagus.direct_active.run")


def test_uncertain_typing_module_alias_store_invalidates_later_guard_imports(bench, resolver):
	alias_prefixes = (
		"if enabled:\n    import typing as original\n    original.TYPE_CHECKING = True\n",
		"import typing as original\nif enabled:\n    original = None\noriginal.TYPE_CHECKING = True\n",
	)
	for index, prefix in enumerate(alias_prefixes):
		source = (
			prefix + "import typing as flags\ndef run(): return 'old'\n"
			"if flags.TYPE_CHECKING:\n    def run(): return 'replacement'\n"
		)
		assert find_definition(ast.parse(source), "run") is None
		assert extract_function(source, "run") is None
		write(bench / f"nagus/nagus/uncertain_alias_{index}.py", source)
		with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
			resolver.resolve(f"nagus.uncertain_alias_{index}.run")


def test_historical_method_metadata_does_not_select_a_later_same_named_class(bench):
	source = (
		"class Base:\n    def validate(self): return 'historical'\n"
		"class Controller(Base): pass\nclass Base:\n    def validate(self): return 'later'\n"
	)
	assert extract_function(source, "Base.validate") is None
	write(bench / "erpnext/erpnext/bases.py", source)
	doc = annotation(method="Base.validate", path="erpnext/bases.py")
	register_class(
		bench,
		"from erpnext.bases import Controller\nclass CustomLedger(Controller):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.errors
	assert any("ambiguous" in error.lower() and "Base.validate" in error for error in result.errors)


def test_typing_only_class_binding_does_not_hide_an_ancestor_method(bench):
	write(
		bench / "erpnext/erpnext/bases.py",
		"""
		class Base:
		    def validate(self): pass
		class Controller(Base):
		    from typing import TYPE_CHECKING
		    if TYPE_CHECKING:
		        def validate(self): ...
		""",
	)
	register_class(
		bench,
		"from erpnext.bases import Controller\nclass CustomLedger(Controller):\n    def validate(self): pass\n",
	)
	result = validate(bench)
	assert result.checked == 1
	assert len(result.errors) == 1
	assert "Missing annotation fields" in result.errors[0]
	assert "erpnext.bases.Base.validate" in result.errors[0]


def test_module_guard_method_else_agrees_across_consumers(bench, resolver):
	source = (
		"from typing import TYPE_CHECKING\nclass Ledger:\n    if TYPE_CHECKING:\n"
		"        def validate(self): return 'typing-only'\n    else:\n"
		"        def validate(self): return 'runtime'\n"
	)
	extracted = extract_function(source, "Ledger.validate")
	assert extracted is not None and "return 'runtime'" in extracted
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py", source)
	method = resolver.resolve("erpnext.accounts.doctype.ledger.ledger.Ledger.validate")
	assert ast.literal_eval(method.node.body[0].value) == "runtime"
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\n"
		"class CustomLedger(Ledger):\n    def validate(self): pass\n",
	)
	result = validate(bench)
	assert result.checked == 1
	assert len(result.errors) == 1
	assert "Missing annotation fields" in result.errors[0]
	assert "Ledger.validate" in result.errors[0]


def test_class_local_guard_rebinding_remains_unknown(bench, resolver):
	source = (
		"from typing import TYPE_CHECKING\nclass Ledger:\n    TYPE_CHECKING = dynamic()\n"
		"    if TYPE_CHECKING:\n        def validate(self): return 'first'\n"
		"    else:\n        def validate(self): return 'second'\n"
	)
	assert extract_function(source, "Ledger.validate") is None
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py", source)
	with pytest.raises(ResolutionError, match=r"[Aa]mbiguous"):
		resolver.resolve("erpnext.accounts.doctype.ledger.ledger.Ledger.validate")
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\n"
		"class CustomLedger(Ledger):\n    def validate(self): pass\n",
	)
	result = validate(bench)
	assert result.checked == 0
	assert result.errors
	assert all("Missing annotation fields" not in error for error in result.errors)


def test_complete_qualified_annotation_passes_after_a_circular_wildcard(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger:\n    def validate(self): pass\nfrom .api import *\n",
	)
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/api.py", "from .ledger import Ledger\n")
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors
