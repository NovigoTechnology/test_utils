"""Captured class identity, C3 selection, and controller inventory contracts."""

import ast
import json
import subprocess
import sys

import pytest
from test_validate_override_annotations import annotation, function_source, register_class, validate, write
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, extract_function, runtime_body, static_exports


def circular_classes(bench, import_style, replacement_method="later"):
	statement = "from .api import File" if import_style == "explicit" else "from .api import *"
	module(
		bench,
		"controller",
		"class Base:\n    def validate(self): return 'captured'\n"
		+ statement
		+ f"\nclass Base:\n    def {replacement_method}(self): return 'replacement'",
	)
	module(bench, "api", "from .controller import Base\n__all__ = ['File']\nclass File(Base): pass")


@pytest.mark.parametrize("cache_order", [(None, 3), (3, None)])
def test_circular_subclass_retains_its_original_base_after_import_returns(bench, cache_order):
	import_style = "explicit"
	circular_classes(bench, import_style)
	source_resolver = resolver(bench)
	controller = source_resolver.imported_module("erpnext.controller")
	for before in cache_order:
		source_resolver.analysis(controller, before)
	selected = source_resolver.resolve("erpnext.controller.File")
	for before in reversed(cache_order):
		source_resolver.analysis(controller, before)
	for _ in range(2):
		hierarchy = source_resolver.hierarchy(selected)
		assert [(value.reference, value.node.lineno) for value in hierarchy] == [
			("erpnext.api.File", 3),
			("erpnext.controller.Base", 1),
		]
		assert source_resolver.member(hierarchy[1], "validate").node.lineno == 2
		assert source_resolver.member(hierarchy[1], "later") is None
		assert source_resolver.resolve("erpnext.controller.Base").node.lineno == 4
		assert source_resolver.resolve("erpnext.controller.File.validate").node.lineno == 2
	assert not any(source_resolver.import_limits.values())


def test_wildcard_circular_subclass_keeps_its_import_snapshot(bench):
	import_style = "wildcard"
	cache_order = (None, 3)
	circular_classes(bench, import_style)
	source_resolver = resolver(bench)
	controller = source_resolver.imported_module("erpnext.controller")
	for before in cache_order:
		source_resolver.analysis(controller, before)
	selected = source_resolver.resolve("erpnext.controller.File")
	for before in reversed(cache_order):
		source_resolver.analysis(controller, before)
	for _ in range(2):
		hierarchy = source_resolver.hierarchy(selected)
		assert [(value.reference, value.node.lineno) for value in hierarchy] == [
			("erpnext.api.File", 3),
			("erpnext.controller.Base", 1),
		]
		assert source_resolver.member(hierarchy[1], "validate").node.lineno == 2
		assert source_resolver.member(hierarchy[1], "later") is None
		assert source_resolver.resolve("erpnext.controller.Base").node.lineno == 4
		assert source_resolver.resolve("erpnext.controller.File.validate").node.lineno == 2
	assert not any(source_resolver.import_limits.values())


def test_validator_reports_override_in_the_base_captured_by_a_circular_subclass(bench):
	import_style = "wildcard"
	circular_classes(bench, import_style)
	register_class(
		bench, "from erpnext.controller import File\nclass CustomLedger(File):\n    def validate(self): pass"
	)
	result = validate(bench)
	assert result.checked == 1
	assert any("Missing annotation fields" in error for error in result.errors)


def test_metadata_for_a_later_redefined_base_cannot_validate_a_new_custom_helper(bench):
	import_style = "explicit"
	circular_classes(bench, import_style)
	register_class(
		bench,
		"from erpnext.controller import File\nclass CustomLedger(File):\n"
		+ function_source(
			annotation(method="Base.later", path="erpnext/controller.py"), name="later", indent="    "
		),
	)
	assert any("no longer exists" in error for error in validate(bench).errors)


def test_captured_method_metadata_reports_ambiguous_active_lookup(bench):
	module(
		bench,
		"api",
		"class Base:\n    def validate(self): pass\nclass Ledger(Base): pass\nif enabled:\n    class Base:\n        def validate(self): pass",
	)
	register_class(
		bench,
		"from erpnext.api import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(
			annotation(method="Base.validate", path="erpnext/api.py"), name="validate", indent="    "
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert any("Ambiguous METHOD 'Base.validate'" in error for error in result.errors)
	assert any("conditional declaration of Base" in error for error in result.errors)


def test_captured_method_metadata_distinguishes_missing_reexport_from_ambiguous_lookup(bench):
	module(
		bench,
		"api",
		"class Base:\n    def validate(self): pass\nclass Ledger(Base): pass\nfrom .replacement import Base",
	)
	module(bench, "replacement", "class Base: pass")
	register_class(
		bench,
		"from erpnext.api import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(
			annotation(method="Base.validate", path="erpnext/api.py"), name="validate", indent="    "
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_captured_method_metadata_rejects_an_unavailable_active_import_source(bench):
	module(
		bench,
		"api",
		"class Base:\n    def validate(self): pass\nclass Ledger(Base): pass\nfrom .unavailable import Base",
	)
	register_class(
		bench,
		"from erpnext.api import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(
			annotation(method="Base.validate", path="erpnext/api.py"), name="validate", indent="    "
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert any("Ambiguous METHOD 'Base.validate'" in error for error in result.errors)
	assert any("Source not found for imported module erpnext.unavailable" in error for error in result.errors)


def test_circular_snapshot_keeps_transitive_bases_and_c3_precedence(bench):
	import_style = "wildcard"
	statement = "from .api import File" if import_style == "explicit" else "from .api import *"
	module(
		bench,
		"controller",
		"class Root:\n    def validate(self): return 'captured'\nclass Base(Root): pass\n"
		+ statement
		+ "\nclass Root:\n    def validate(self): return 'replacement'\nclass Base(Root): pass",
	)
	module(
		bench,
		"api",
		"from .controller import Base\n__all__ = ['File']\nclass Mixin: pass\nclass File(Mixin, Base): pass",
	)
	source_resolver = resolver(bench)
	selected = source_resolver.resolve("erpnext.controller.File")
	assert [(value.reference, value.node.lineno) for value in source_resolver.hierarchy(selected)] == [
		("erpnext.api.File", 4),
		("erpnext.api.Mixin", 3),
		("erpnext.controller.Base", 3),
		("erpnext.controller.Root", 1),
	]


def test_circular_snapshot_agrees_with_python_for_the_selected_import_entry(bench):
	import_style = "explicit"
	circular_classes(bench, import_style)
	program = "\n".join(
		[
			"import json, sys",
			"sys.path.insert(0, sys.argv[1])",
			"from erpnext.controller import File, Base",
			"captured = File.__mro__[1]",
			"print(json.dumps([captured.validate.__code__.co_firstlineno, captured is Base]))",
		]
	)
	actual = subprocess.run(
		[sys.executable, "-I", "-c", program, str(bench / "erpnext")],
		check=True,
		capture_output=True,
		text=True,
	)
	assert json.loads(actual.stdout) == [2, False]
	source_resolver = resolver(bench)
	selected = source_resolver.resolve("erpnext.controller.File")
	captured = source_resolver.hierarchy(selected)[1]
	assert source_resolver.member(captured, "validate").node.lineno == 2
	assert captured.identity != source_resolver.resolve("erpnext.controller.Base").identity


def test_c3_merges_a_captured_old_base_with_a_later_subclass_of_that_same_base(bench):
	import_style = "wildcard"
	statement = "from .api import File" if import_style == "explicit" else "from .api import *"
	module(
		bench,
		"controller",
		"class Base:\n    def validate(self): return 'original'\n"
		+ statement
		+ "\nclass Base(Base): pass\nclass Ledger(File, Base): pass",
	)
	module(bench, "api", "from .controller import Base\n__all__ = ['File']\nclass File(Base): pass")
	source_resolver = resolver(bench)
	assert [
		(value.reference, value.node.lineno)
		for value in source_resolver.hierarchy(source_resolver.resolve("erpnext.controller.Ledger"))
	] == [
		("erpnext.controller.Ledger", 5),
		("erpnext.api.File", 3),
		("erpnext.controller.Base", 4),
		("erpnext.controller.Base", 1),
	]


def test_same_file_metadata_cannot_identify_a_shadowed_inherited_definition(bench):
	import_style = "wildcard"
	circular_classes(bench, import_style, replacement_method="validate")
	register_class(
		bench,
		"from erpnext.controller import File\nclass CustomLedger(File):\n"
		+ function_source(
			annotation(method="Base.validate", path="erpnext/controller.py"), name="validate", indent="    "
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert any("Ambiguous METHOD 'Base.validate'" in error for error in result.errors)
	assert any("later definition in the same file" in error for error in result.errors)


def test_direct_inherited_lookup_returns_the_declaring_class(bench):
	module(bench, "api", "class Base:\n    def target(self): return 'base'\nclass Child(Base): pass")
	assert resolver(bench).resolve("erpnext.api.Child.target").qualname == "Base.target"


def test_direct_inherited_lookup_follows_c3_declarations_across_a_diamond(bench):
	module(
		bench,
		"api",
		"class A:\n    def target(self): return 'A'\nclass B(A): pass\nclass C(A):\n    def target(self): return 'C'\nclass D(B, C): pass",
	)
	assert resolver(bench).resolve("erpnext.api.D.target").qualname == "C.target"


def test_direct_inherited_lookup_rejects_the_invalidated_declaring_callable(bench):
	mutation = "Base.target.__code__ = replacement.__code__"
	module(
		bench,
		"api",
		"class Base:\n    def target(self, *, option=1): return 'old'\nclass Child(Base): pass\nclass Final(Child): pass\ndef replacement(self, *, option=2): return 'new'\n"
		+ mutation,
	)
	source_resolver = resolver(bench)
	for owner in ("Child", "Final"):
		with pytest.raises(ResolutionError, match="Ambiguous"):
			source_resolver.resolve(f"erpnext.api.{owner}.target")


def test_direct_inherited_lookup_stops_at_shadowing_declarations(bench):
	declaration = "target = 3"
	module(
		bench,
		"api",
		"class A:\n    def target(self): return 'A'\nclass B(A):\n    " + declaration + "\nclass D(B): pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.D.target")


def test_c3_linearization_matches_python_for_redefined_classes(bench):
	source = "class Base: marker = 'original'\nclass Old(Base): marker = 'old-child'\nclass Base: marker = 'replacement'\nclass File(Old, Base): marker = 'file'"
	namespace = {}
	exec(compile(source, "<inheritance-oracle>", "exec"), namespace)
	actual = [owner.__dict__["marker"] for owner in namespace["File"].__mro__[:-1]]
	module(bench, "api", source)
	source_resolver = resolver(bench)
	resolved = source_resolver.hierarchy(source_resolver.resolve("erpnext.api.File"))
	assert [ast.literal_eval(owner.node.body[0].value) for owner in resolved] == actual


def test_dynamically_added_custom_override_is_included_in_the_inventory(bench):
	mutation = "CustomLedger.validate = replacement"
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger): pass\ndef replacement(self): pass\n"
		+ mutation,
	)
	assert any("Ambiguous" in error for error in validate(bench).errors)


def test_dynamically_added_upstream_method_cannot_be_treated_as_a_new_custom_helper(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger: pass\ndef replacement(self): pass\nLedger.validate = replacement",
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n    def validate(self): pass",
	)
	assert any("Ambiguous" in error for error in validate(bench).errors)


def test_dynamic_custom_helper_without_an_upstream_attribute_remains_a_helper(bench):
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger): pass\ndef replacement(self): pass\nCustomLedger.helper = replacement",
	)
	assert not validate(bench).errors
