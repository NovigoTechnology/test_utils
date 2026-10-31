"""Independent resolver and historical-extractor regression cases for PR #11."""

import ast

import pytest
from test_validate_override_annotations import (
	annotation,
	function_source,
	register_class,
	validate,
	write,
)
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import extract_function


@pytest.mark.parametrize(
	"source",
	[
		"def overload(fn): return fn\ndef target(): return 'old'\n@overload\ndef target(): return 'new'",
		"from typing import overload as signature\ndef identity(fn): return fn\nsignature = identity\ndef target(): return 'old'\n@signature\ndef target(): return 'new'",
		"def signature(fn): return fn\ndef target(): return 'old'\n@signature\ndef target(): return 'new'\nfrom typing import overload as signature",
	],
)
def test_runtime_decorators_are_not_mistaken_for_typing_overloads(bench, source):
	module(bench, "api", source)
	selected = resolver(bench).resolve("erpnext.api.target")
	assert ast.literal_eval(selected.node.body[0].value) == "new"
	assert "return 'new'" in extract_function(source, "target")


def test_class_local_overload_alias_uses_its_definition_time_binding(bench):
	source = "from typing import overload as signature\ndef identity(fn): return fn\nclass File:\n    signature = identity\n    @signature\n    def target(self): return 'new'"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.File.target").node.lineno == 6
	assert "return 'new'" in extract_function(source, "File.target")


def test_rebinding_typing_alias_after_overload_keeps_the_original_stub_identity(bench):
	source = "from typing import overload as signature\n@signature\ndef target(value: str): ...\ndef target(value): return 'runtime'\nsignature = object"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 4
	assert "return 'runtime'" in extract_function(source, "target")


def test_uncertain_typing_overload_binding_cannot_certify_a_stub(bench):
	source = "from typing import overload as signature\nif enabled:\n    signature = unknown\n@signature\ndef target(): return 'maybe'"
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


@pytest.mark.parametrize(
	"mutation",
	[
		"typing.overload = identity",
		"setattr(typing, 'overload', identity)",
		"def configure(t):\n    t.overload = identity\nconfigure(typing)",
		"typing.overload = identity\nfrom typing import overload as signature",
	],
)
def test_mutated_typing_overload_attributes_are_not_treated_as_standard_stubs(bench, mutation):
	decorator = "signature" if "as signature" in mutation else "typing.overload"
	source = f"import typing\ndef identity(fn): return fn\n{mutation}\n@{decorator}\ndef target(): return 'runtime'"
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_copied_overload_retains_its_identity_after_typing_module_mutation(bench):
	source = "import typing\nfrom typing import overload as signature\ntyping.overload = unknown\n@signature\ndef target(): ...\ndef target(): return 'runtime'"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 6
	assert "return 'runtime'" in extract_function(source, "target")


def test_replaced_nested_method_does_not_replace_same_named_module_function(bench):
	source = "def validate(): return 'module'\ndef replacement(fn): return None\nclass File:\n    @replacement\n    def validate(self): return 'nested'"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	assert source_resolver.resolve("erpnext.api.validate").node.lineno == 1
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve("erpnext.api.File.validate")
	assert "return 'module'" in extract_function(source, "validate")
	assert extract_function(source, "File.validate") is None


@pytest.mark.parametrize(
	"source",
	[
		"class Ledger:\n    def validate(self): return 'old'\nclass Ledger:\n    def validate(self): return 'active'",
		"class Shadowed:\n    def validate(self): return 'stale'\nShadowed = object\nclass Ledger:\n    def validate(self): return 'active'",
		"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    class Shadowed:\n        def validate(self): return 'stale'\nclass Ledger:\n    def validate(self): return 'active'",
	],
)
def test_legacy_method_lookup_counts_only_active_classes(bench, source):
	module(bench, "api", source)
	source_resolver = resolver(bench)
	selected = source_resolver.resolve("erpnext.api.Ledger")
	assert source_resolver.function_symbol(selected.module, "validate").qualname == "Ledger.validate"
	assert "return 'active'" in extract_function(source, "validate")


def test_legacy_method_metadata_is_valid_after_same_name_class_redefinition(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"class Ledger:\n    def validate(self): return 'old'\nclass Ledger:\n    def validate(self): return 'active'",
	)
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(
			annotation(method="validate", path="erpnext/accounts/doctype/ledger/ledger.py"),
			name="validate",
			indent="    ",
		),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


@pytest.mark.parametrize(
	"mutation",
	[
		"Ledger.validate = replacement",
		"setattr(Ledger, 'validate', replacement)",
		"def configure(cls):\n    cls.validate = replacement\nconfigure(Ledger)",
		"alias = Ledger\nalias.validate = replacement",
		"setattr(Ledger, dynamic_name, replacement)",
	],
)
def test_import_time_member_replacement_cannot_reuse_old_method_metadata(bench, mutation):
	source = (
		"class Ledger:\n    def validate(self): return 'old'\ndef replacement(self): return 'new'\n"
		+ mutation
	)
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.Ledger.validate")
	assert extract_function(source, "Ledger.validate") is None


def test_member_mutation_does_not_invalidate_an_unrelated_method(bench):
	source = "class Ledger:\n    def validate(self): return 'old'\n    def reconcile(self): return 'stable'\nLedger.validate = object"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.Ledger.reconcile").node.lineno == 3
	assert "return 'stable'" in extract_function(source, "Ledger.reconcile")


def test_mutating_a_redefined_class_does_not_invalidate_the_selected_earlier_base(bench):
	source = "class Base:\n    def validate(self): return 'original'\nclass Ledger(Base): pass\nclass Base:\n    def validate(self): return 'replacement'\nBase.validate = object"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	base = source_resolver.hierarchy(source_resolver.resolve("erpnext.api.Ledger"))[1]
	assert source_resolver.member(base, "validate").node.lineno == 2


@pytest.mark.parametrize(
	"mutation",
	[
		"CustomLedger.validate = replacement",
		"setattr(CustomLedger, 'validate', replacement)",
		"setattr(CustomLedger, unknown_name, replacement)",
	],
)
def test_dynamically_added_custom_override_is_included_in_the_inventory(bench, mutation):
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


@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize(
	"mutation",
	[
		"target.__code__ = replacement.__code__",
		"setattr(target, '__code__', replacement.__code__)",
		"target.__defaults__ = (2,)",
		"target.__kwdefaults__ = {'option': 2}",
		"target.__doc__ = 'replacement metadata'",
		"target.__wrapped__ = replacement",
		"def configure(value):\n    value.__code__ = replacement.__code__\nconfigure(target)",
	],
)
def test_modified_callable_cannot_certify_its_original_ast(bench, qualified, mutation):
	source = "def replacement(value=2, *, option=2): return 'new'\n"
	if qualified:
		source += "class File:\n    def validate(self, value=1, *, option=1): return 'old'\n"
		mutation = mutation.replace("target", "File.validate")
		name = "File.validate"
	else:
		source += "def target(value=1, *, option=1): return 'old'\n"
		name = "target"
	source += mutation
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve(f"erpnext.api.{name}")
	assert extract_function(source, name) is None


def test_function_metadata_mutation_keeps_an_unrelated_callable_resolvable(bench):
	source = "def target(): return 'stable'\ndef replacement(): return 'new'\nreplacement.__doc__ = 'changed'"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 1
	assert "return 'stable'" in extract_function(source, "target")


def test_function_mutation_on_a_redefined_class_preserves_the_earlier_base(bench):
	source = "class Base:\n    def validate(self): return 'original'\nclass Ledger(Base): pass\nclass Base:\n    def validate(self): return 'replacement'\ndef replacement(self): return 'patched'\nBase.validate.__code__ = replacement.__code__"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	base = source_resolver.hierarchy(source_resolver.resolve("erpnext.api.Ledger"))[1]
	assert source_resolver.member(base, "validate").node.lineno == 2
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve("erpnext.api.Base.validate")


def test_mutating_a_captured_old_method_invalidates_that_base_after_class_rebinding(bench):
	source = "class Base:\n    def validate(self): return 'original'\noriginal = Base.validate\nclass Ledger(Base): pass\nclass Base:\n    def validate(self): return 'replacement'\noriginal.__doc__ = 'changed'"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	base = source_resolver.hierarchy(source_resolver.resolve("erpnext.api.Ledger"))[1]
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.member(base, "validate")
	assert source_resolver.resolve("erpnext.api.Base.validate").node.lineno == 6
	assert "return 'replacement'" in extract_function(source, "Base.validate")


@pytest.mark.parametrize("limited_first", [False, True])
def test_function_mutation_analysis_is_cached_per_module_snapshot(bench, limited_first):
	source = "class File:\n    def validate(self): return 'old'\ndef replacement(self): return 'new'\nFile.validate.__code__ = replacement.__code__"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	owner = source_resolver.resolve("erpnext.api.File")

	def limited():
		assert source_resolver.qualified(owner.module, "File.validate", before=4).node.lineno == 2

	def complete():
		with pytest.raises(ResolutionError, match="Ambiguous"):
			source_resolver.qualified(owner.module, "File.validate")

	for check in (limited, complete) if limited_first else (complete, limited):
		check()
