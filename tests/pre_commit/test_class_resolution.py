"""Circular-import inheritance snapshots from Copilot review 5720110068."""

import json
import subprocess
import sys

import pytest
from test_validate_override_annotations import (
	annotation,
	function_source,
	register_class,
	validate,
)
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError


def circular_classes(bench, import_style, replacement_method="later"):
	statement = "from .api import File" if import_style == "explicit" else "from .api import *"
	module(
		bench,
		"controller",
		"class Base:\n    def validate(self): return 'captured'\n"
		+ statement
		+ f"\nclass Base:\n    def {replacement_method}(self): return 'replacement'",
	)
	module(
		bench,
		"api",
		"from .controller import Base\n__all__ = ['File']\nclass File(Base): pass",
	)


@pytest.mark.parametrize("import_style", ["explicit", "wildcard"])
@pytest.mark.parametrize("cache_order", [(None, 3), (3, None)])
def test_circular_subclass_retains_its_original_base_after_import_returns(bench, import_style, cache_order):
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


@pytest.mark.parametrize("import_style", ["explicit", "wildcard"])
def test_validator_reports_override_in_the_base_captured_by_a_circular_subclass(bench, import_style):
	circular_classes(bench, import_style)
	register_class(
		bench,
		"from erpnext.controller import File\nclass CustomLedger(File):\n    def validate(self): pass",
	)
	result = validate(bench)
	assert result.checked == 1
	assert any("Missing annotation fields" in error for error in result.errors)


@pytest.mark.parametrize("import_style", ["explicit", "wildcard"])
def test_metadata_for_a_later_redefined_base_cannot_validate_a_new_custom_helper(bench, import_style):
	circular_classes(bench, import_style)
	register_class(
		bench,
		"from erpnext.controller import File\nclass CustomLedger(File):\n"
		+ function_source(
			annotation(method="Base.later", path="erpnext/controller.py"), name="later", indent="    "
		),
	)
	assert any("no longer exists" in error for error in validate(bench).errors)


@pytest.mark.parametrize("import_style", ["explicit", "wildcard"])
def test_circular_snapshot_keeps_transitive_bases_and_c3_precedence(bench, import_style):
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


@pytest.mark.parametrize("import_style", ["explicit", "wildcard"])
def test_circular_snapshot_agrees_with_python_for_the_selected_import_entry(bench, import_style):
	circular_classes(bench, import_style)
	# Execute only these generated fixtures, never application checkouts.
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


@pytest.mark.parametrize("import_style", ["explicit", "wildcard"])
def test_c3_merges_a_captured_old_base_with_a_later_subclass_of_that_same_base(bench, import_style):
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


@pytest.mark.parametrize("import_style", ["explicit", "wildcard"])
def test_same_file_metadata_cannot_identify_a_shadowed_inherited_definition(bench, import_style):
	circular_classes(bench, import_style, replacement_method="validate")
	register_class(
		bench,
		"from erpnext.controller import File\nclass CustomLedger(File):\n"
		+ function_source(
			annotation(method="Base.validate", path="erpnext/controller.py"),
			name="validate",
			indent="    ",
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


@pytest.mark.parametrize(
	"mutation", ["Base.target.__code__ = replacement.__code__", "Base.target.__kwdefaults__ = {'option': 2}"]
)
def test_direct_inherited_lookup_rejects_the_invalidated_declaring_callable(bench, mutation):
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


@pytest.mark.parametrize("declaration", ["target = 3", "def target(self): return 'B'\n    del target"])
def test_direct_inherited_lookup_stops_at_shadowing_declarations(bench, declaration):
	module(
		bench,
		"api",
		"class A:\n    def target(self): return 'A'\nclass B(A):\n    " + declaration + "\nclass D(B): pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.D.target")
