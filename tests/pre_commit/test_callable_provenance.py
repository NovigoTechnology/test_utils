"""Callable identity and ownership of mutable defaults, with safe-copy positives."""

import ast

import pytest
from test_validate_override_annotations import annotation, function_source, register_class, validate, write
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import AmbiguousBinding, extract_function, runtime_body, static_exports


def callable_source(qualified):
	if qualified:
		return ("class File:\n    def target(self, *, option=1): return 'original'\n", "File.target")
	return ("def target(*, option=1): return 'original'\n", "target")


def assert_metadata_write_rejects_original_ast(bench, operation, *, qualified=False):
	source = "def replacement(value=2, *, option=2): return 'new'\n"
	if qualified:
		source += "class File:\n    def validate(self, value=1, *, option=1): return 'old'\n"
		name = "File.validate"
	else:
		source += "def target(value=1, *, option=1): return 'old'\n"
		name = "target"
	source += operation.replace("target", name)
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve(f"erpnext.api.{name}")
	assert extract_function(source, name) is None


def assert_default_mutation_rejects_owner(bench, *, qualified, operation):
	source, name = callable_source(qualified)
	source += operation.replace("target", name)
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve(f"erpnext.api.{name}")
	assert extract_function(source, name) is None


def assert_default_read_preserves_owner(bench, *, qualified, operation):
	source, name = callable_source(qualified)
	source += operation.replace("target", name)
	module(bench, "api", source)
	selected = resolver(bench).resolve(f"erpnext.api.{name}")
	assert ast.literal_eval(selected.node.body[0].value) == "original"
	assert "return 'original'" in extract_function(source, name)


def test_modified_callable_metadata_cannot_certify_its_original_ast(bench):
	operation = "target.__code__ = replacement.__code__"
	assert_metadata_write_rejects_original_ast(bench, operation)


def test_module_identity_decorator_is_preserved_during_member_resolution(bench):
	module(
		bench,
		"api",
		"def identity(fn):\n    return fn\nclass File:\n    @identity\n    def validate(self): pass",
	)
	assert resolver(bench).resolve("erpnext.api.File.validate").node.name == "validate"


def test_module_decorator_replacing_a_method_is_not_lost_in_member_resolution(bench):
	module(
		bench,
		"api",
		"def replacement(self): pass\ndef replace(fn):\n    return replacement\nclass File:\n    @replace\n    def validate(self): pass",
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.File.validate")


def test_replaced_nested_method_does_not_replace_same_named_module_function(bench):
	source = "def validate(): return 'module'\ndef replacement(fn): return None\nclass File:\n    @replacement\n    def validate(self): return 'nested'"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	assert source_resolver.resolve("erpnext.api.validate").node.lineno == 1
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve("erpnext.api.File.validate")
	assert "return 'module'" in extract_function(source, "validate")
	assert extract_function(source, "File.validate") is None


def test_runtime_decorators_are_not_mistaken_for_typing_overloads(bench):
	source = "def overload(fn): return fn\ndef target(): return 'old'\n@overload\ndef target(): return 'new'"
	module(bench, "api", source)
	selected = resolver(bench).resolve("erpnext.api.target")
	assert ast.literal_eval(selected.node.body[0].value) == "new"
	assert "return 'new'" in extract_function(source, "target")


def test_copied_overload_retains_its_identity_after_typing_module_mutation(bench):
	source = "import typing\nfrom typing import overload as signature\ntyping.overload = unknown\n@signature\ndef target(): ...\ndef target(): return 'runtime'"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 6
	assert "return 'runtime'" in extract_function(source, "target")


def test_uncertain_typing_receiver_cannot_hide_a_replacement_definition(bench):
	setup = "if enabled:\n    t = CustomTyping"
	decorator = "t.overload"
	source = (
		"import typing as t\nclass CustomTyping:\n    @staticmethod\n    def overload(function): return function\n"
		+ setup
		+ "\ndef target(): return 'old'\n"
		+ f"@{decorator}\ndef target(): return 'replacement'"
	)
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_captured_real_overload_keeps_its_identity_after_receiver_rebinding(bench):
	conditional = False
	rebinding = "if enabled:\n    t = CustomTyping" if conditional else "t = CustomTyping"
	source = (
		"import typing as t\nsignature = t.overload\nclass CustomTyping:\n    @staticmethod\n    def overload(function): return function\n"
		+ rebinding
		+ "\ndef target(): return 'original'\n"
		+ "@signature\ndef target(): ..."
	)
	module(bench, "api", source)
	selected = resolver(bench).resolve("erpnext.api.target")
	assert ast.literal_eval(selected.node.body[0].value) == "original"
	assert "return 'original'" in extract_function(source, "target")


def test_mutating_keyword_default_metadata_invalidates_its_owner(bench):
	operation = "target.__kwdefaults__['option'] = 2"
	assert_default_mutation_rejects_owner(bench, qualified=False, operation=operation)


def test_keyword_default_alias_keeps_the_owning_definition(bench):
	operation = "defaults = target.__kwdefaults__\ndefaults['option'] = 2"
	assert_default_mutation_rejects_owner(bench, qualified=False, operation=operation)


def test_reading_or_copying_default_metadata_preserves_the_owner(bench):
	operation = "value = target.__kwdefaults__.get('option')\nsnapshot = target.__kwdefaults__.copy()\nsnapshot['option'] = 2"
	assert_default_read_preserves_owner(bench, qualified=False, operation=operation)


def test_mutating_a_shared_mutable_default_invalidates_its_owner(bench):
	projection = "target.__kwdefaults__.copy()['option']"
	module(
		bench, "api", "def target(*, option=[]): return option\nalias = " + projection + "\nalias.append(2)"
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")


def test_mutation_through_an_original_alias_invalidates_the_captured_default(bench):
	setup = "options = []\ndef target(*, option=options): return option"
	mutation = "options.append(2)"
	source = setup + "\n" + mutation
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_shared_default_invalidates_each_owner_without_invalidating_an_independent_function(bench):
	source = "options = []\ndef first(option=options): return option\ndef second(*, option=options): return option\ndef independent(option=[]): return option\noptions.append(2)"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve(f"erpnext.api.{'first'}")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve(f"erpnext.api.{'second'}")
	assert source_resolver.resolve("erpnext.api.independent").node.name == "independent"


@pytest.mark.parametrize(
	"source",
	[
		"options = []\ndef target(option=options.copy()): return option\noptions.append(2)",
		"options = []\noptions.append(2)\ndef target(option=options): return option",
		"options = []\ndef target(option=options): return option\noptions = []\noptions.append(2)",
	],
	ids=["independent-copy", "before-capture", "rebound-alias"],
)
def test_operations_that_preserve_the_captured_default_keep_the_definition(bench, source):
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.name == "target"
	assert extract_function(source, "target") is not None


def test_imported_mutable_default_keeps_identity_across_import_forms(bench):
	mutation = "import erpnext.config\nerpnext.config.options.append(2)"
	module(bench, "config", "options = []")
	source = "from erpnext.config import options\ndef target(option=options): return option\n" + mutation
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_default_dictionary_lookup_with_a_callback_key_cannot_certify_old_defaults(bench):
	source = "def target(*, option=[]): return option\nclass Key:\n    def __hash__(self):\n        target.__kwdefaults__['option'].append(2)\n        return 0\nvalue = target.__kwdefaults__.get(Key())"
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_mutated_defaults_follow_the_c3_selected_owner(bench):
	source = "class Root:\n    def target(self, *, option=1): return 'root'\nclass Left(Root): pass\nclass Right(Root):\n    def target(self, *, option=1): return 'right'\nclass Final(Left, Right): pass\nFinal.target.__kwdefaults__['option'] = 2"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	assert source_resolver.resolve("erpnext.api.Root.target").node.lineno == 2
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve("erpnext.api.Right.target")
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve("erpnext.api.Final.target")
	assert "return 'root'" in extract_function(source, "Root.target")
	assert extract_function(source, "Right.target") is None


def test_captured_defaults_keep_the_original_definition_after_class_rebinding(bench):
	source = "class Base:\n    def target(self, *, option=1): return 'original'\ndefaults = Base.target.__kwdefaults__\nclass Child(Base): pass\nclass Base:\n    def target(self, *, option=1): return 'replacement'\ndefaults['option'] = 2"
	module(bench, "api", source)
	source_resolver = resolver(bench)
	assert source_resolver.resolve("erpnext.api.Base.target").node.lineno == 6
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve("erpnext.api.Child.target")
	assert "return 'replacement'" in extract_function(source, "Base.target")


def test_unconsumed_generator_does_not_mutate_default_metadata(bench):
	source = "def target(*, option=1): return 'original'\ndef configure():\n    target.__kwdefaults__['option'] = 2\n    yield None\npending = configure()"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 1
	assert "return 'original'" in extract_function(source, "target")


def test_consumed_deferred_results_keep_the_default_metadata_owner(bench):
	operation = "async def defaults(function):\n    return function.__kwdefaults__\nasync def configure():\n    mapping = await defaults(target)\n    mapping['option'] = 2\npending = configure()\ntry:\n    pending.send(None)\nexcept StopIteration:\n    pass"
	source = "def target(*, option=1): return 'original'\n" + operation
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_function_metadata_mutation_keeps_an_unrelated_callable_resolvable(bench):
	source = "def target(): return 'stable'\ndef replacement(): return 'new'\nreplacement.__doc__ = 'changed'"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 1
	assert "return 'stable'" in extract_function(source, "target")


def test_mutating_one_methods_default_metadata_keeps_a_sibling_proven(bench):
	source = "class File:\n    def target(self, *, option=1): return 'original'\n    def other(self, *, option=1): return 'other'\nFile.other.__kwdefaults__.clear()"
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.File.target").node.lineno == 2
	assert "return 'original'" in extract_function(source, "File.target")
