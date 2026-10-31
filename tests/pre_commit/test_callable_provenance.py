"""Callable ownership, mutable defaults, and decorator receiver provenance.

The typing examples are inspected as syntax only.  No test executes mutations
against typing or imports an application module.
"""

import ast

import pytest
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import extract_function

CALLABLE_METADATA_WRITES = [
	("code", "target.__code__ = replacement.__code__"),
	("setattr-code", "setattr(target, '__code__', replacement.__code__)"),
	("defaults", "target.__defaults__ = (2,)"),
	("keyword-defaults", "target.__kwdefaults__ = {'option': 2}"),
	("doc", "target.__doc__ = 'replacement metadata'"),
	("wrapped", "target.__wrapped__ = replacement"),
	("helper-code", "def configure(value):\n    value.__code__ = replacement.__code__\nconfigure(target)"),
]


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


@pytest.mark.parametrize(
	"_name, operation", CALLABLE_METADATA_WRITES, ids=[case[0] for case in CALLABLE_METADATA_WRITES]
)
def test_modified_callable_metadata_cannot_certify_its_original_ast(bench, _name, operation):
	assert_metadata_write_rejects_original_ast(bench, operation)


@pytest.mark.parametrize(
	"_name, operation",
	[case for case in CALLABLE_METADATA_WRITES if case[0] in {"code", "keyword-defaults"}],
	ids=["code", "keyword-defaults"],
)
def test_method_body_and_defaults_metadata_writes_reject_the_original_ast(bench, _name, operation):
	assert_metadata_write_rejects_original_ast(bench, operation, qualified=True)


DEFAULT_MUTATIONS = [
	("item-assignment", "target.__kwdefaults__['option'] = 2"),
	("item-deletion", "del target.__kwdefaults__['option']"),
	("update-keyword", "target.__kwdefaults__.update(option=2)"),
	("update-mapping", "target.__kwdefaults__.update({'option': 2})"),
	("pop", "target.__kwdefaults__.pop('option')"),
	("popitem", "target.__kwdefaults__.popitem()"),
	("clear", "target.__kwdefaults__.clear()"),
	("setitem", "target.__kwdefaults__.__setitem__('option', 2)"),
	("alias-item-assignment", "defaults = target.__kwdefaults__\ndefaults['option'] = 2"),
	("alias-pop", "defaults = target.__kwdefaults__\ndefaults.pop('option')"),
	(
		"nested-defaults-alias",
		"container = {'defaults': target.__kwdefaults__}\ncontainer['defaults']['option'] = 2",
	),
	("projected-list-function", "container = [target]\ncontainer[0].__kwdefaults__['option'] = 2"),
	(
		"projected-dict-function",
		"container = {'function': target}\ncontainer['function'].__kwdefaults__.clear()",
	),
	(
		"returned-defaults",
		"def defaults(function):\n    return function.__kwdefaults__\ndefaults(target)['option'] = 2",
	),
	(
		"returned-function",
		"def identity(function):\n    return function\nidentity(target).__kwdefaults__['option'] = 2",
	),
]


def callable_source(qualified):
	if qualified:
		return "class File:\n    def target(self, *, option=1): return 'original'\n", "File.target"
	return "def target(*, option=1): return 'original'\n", "target"


@pytest.mark.parametrize("_name, operation", DEFAULT_MUTATIONS, ids=[case[0] for case in DEFAULT_MUTATIONS])
def test_mutating_keyword_default_metadata_invalidates_its_owner(bench, _name, operation):
	assert_default_mutation_rejects_owner(bench, qualified=False, operation=operation)


def assert_default_mutation_rejects_owner(bench, *, qualified, operation):
	source, name = callable_source(qualified)
	source += operation.replace("target", name)
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve(f"erpnext.api.{name}")
	assert extract_function(source, name) is None


# Method lookup is a separate boundary; dictionary mutators are shared with
# module functions, so sample direct, projected, and helper-returned receivers.
@pytest.mark.parametrize(
	"_name, operation",
	[
		case
		for case in DEFAULT_MUTATIONS
		if case[0] in {"item-assignment", "projected-dict-function", "returned-defaults"}
	],
	ids=["item-assignment", "projected-dict-function", "returned-defaults"],
)
def test_method_default_mutations_keep_the_defining_owner(bench, _name, operation):
	assert_default_mutation_rejects_owner(bench, qualified=True, operation=operation)


DEFAULT_READS = [
	("getitem", "value = target.__kwdefaults__['option']"),
	("get", "value = target.__kwdefaults__.get('option')"),
	("alias-get", "defaults = target.__kwdefaults__\nvalue = defaults.get('option')"),
	("copy", "snapshot = target.__kwdefaults__.copy()"),
	("copy-then-write", "snapshot = target.__kwdefaults__.copy()\nsnapshot['option'] = 2"),
	("copy-then-clear", "snapshot = target.__kwdefaults__.copy()\nsnapshot.clear()"),
]


@pytest.mark.parametrize("_name, operation", DEFAULT_READS, ids=[case[0] for case in DEFAULT_READS])
def test_reading_or_copying_default_metadata_preserves_the_owner(bench, _name, operation):
	assert_default_read_preserves_owner(bench, qualified=False, operation=operation)


def assert_default_read_preserves_owner(bench, *, qualified, operation):
	source, name = callable_source(qualified)
	source += operation.replace("target", name)
	module(bench, "api", source)
	selected = resolver(bench).resolve(f"erpnext.api.{name}")
	assert ast.literal_eval(selected.node.body[0].value) == "original"
	assert "return 'original'" in extract_function(source, name)


@pytest.mark.parametrize(
	"_name, operation",
	[case for case in DEFAULT_READS if case[0] in {"get", "copy-then-write"}],
	ids=["get", "copy-then-write"],
)
def test_method_metadata_reads_and_independent_copies_preserve_the_owner(bench, _name, operation):
	assert_default_read_preserves_owner(bench, qualified=True, operation=operation)


def test_mutating_default_metadata_keeps_an_unrelated_function_proven(bench):
	source = (
		"def target(*, option=1): return 'original'\n"
		"def other(*, option=1): return 'other'\n"
		"other.__kwdefaults__['option'] = 2"
	)
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 1
	assert "return 'original'" in extract_function(source, "target")


def test_mutating_one_methods_default_metadata_keeps_a_sibling_proven(bench):
	source = (
		"class File:\n"
		"    def target(self, *, option=1): return 'original'\n"
		"    def other(self, *, option=1): return 'other'\n"
		"File.other.__kwdefaults__.clear()"
	)
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.File.target").node.lineno == 2
	assert "return 'original'" in extract_function(source, "File.target")


def test_mutated_defaults_follow_the_c3_selected_owner(bench):
	source = (
		"class Root:\n"
		"    def target(self, *, option=1): return 'root'\n"
		"class Left(Root): pass\n"
		"class Right(Root):\n"
		"    def target(self, *, option=1): return 'right'\n"
		"class Final(Left, Right): pass\n"
		"Final.target.__kwdefaults__['option'] = 2"
	)
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
	source = (
		"class Base:\n"
		"    def target(self, *, option=1): return 'original'\n"
		"defaults = Base.target.__kwdefaults__\n"
		"class Child(Base): pass\n"
		"class Base:\n"
		"    def target(self, *, option=1): return 'replacement'\n"
		"defaults['option'] = 2"
	)
	module(bench, "api", source)
	source_resolver = resolver(bench)
	assert source_resolver.resolve("erpnext.api.Base.target").node.lineno == 6
	with pytest.raises(ResolutionError, match="Ambiguous"):
		source_resolver.resolve("erpnext.api.Child.target")
	assert "return 'replacement'" in extract_function(source, "Base.target")


DEFERRED_DEFAULT_MUTATIONS = [
	(
		"generator-result",
		"def defaults(function):\n    yield function.__kwdefaults__\nnext(defaults(target))['option'] = 2",
	),
	(
		"generator-method-result",
		"def defaults(function):\n    yield function.__kwdefaults__\ndefaults(target).__next__()['option'] = 2",
	),
	(
		"async-result",
		"async def defaults(function):\n    return function.__kwdefaults__\nasync def configure():\n    mapping = await defaults(target)\n    mapping['option'] = 2\npending = configure()\ntry:\n    pending.send(None)\nexcept StopIteration:\n    pass",
	),
]


@pytest.mark.parametrize(
	"_name, operation", DEFERRED_DEFAULT_MUTATIONS, ids=[case[0] for case in DEFERRED_DEFAULT_MUTATIONS]
)
def test_consumed_deferred_results_keep_the_default_metadata_owner(bench, _name, operation):
	source = "def target(*, option=1): return 'original'\n" + operation
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_unconsumed_generator_does_not_mutate_default_metadata(bench):
	source = (
		"def target(*, option=1): return 'original'\n"
		"def configure():\n"
		"    target.__kwdefaults__['option'] = 2\n"
		"    yield None\n"
		"pending = configure()"
	)
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.lineno == 1
	assert "return 'original'" in extract_function(source, "target")


UNCERTAIN_RECEIVERS = [
	("class-receiver", "if enabled:\n    t = CustomTyping", "t.overload"),
	("instance-receiver", "if enabled:\n    t = CustomTyping()", "t.overload"),
	(
		"captured-uncertain-attribute",
		"if enabled:\n    t = CustomTyping\nsignature = t.overload",
		"signature",
	),
	(
		"projected-uncertain-receiver",
		"if enabled:\n    t = CustomTyping\nreceivers = [t]",
		"receivers[0].overload",
	),
]


@pytest.mark.parametrize(
	"_name, setup, decorator", UNCERTAIN_RECEIVERS, ids=[case[0] for case in UNCERTAIN_RECEIVERS]
)
def test_uncertain_typing_receiver_cannot_hide_a_replacement_definition(bench, _name, setup, decorator):
	source = (
		"import typing as t\n"
		"class CustomTyping:\n"
		"    @staticmethod\n"
		"    def overload(function): return function\n"
		+ setup
		+ "\ndef target(): return 'old'\n"
		+ f"@{decorator}\ndef target(): return 'replacement'"
	)
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


@pytest.mark.parametrize("conditional", [False, True])
def test_captured_real_overload_keeps_its_identity_after_receiver_rebinding(bench, conditional):
	rebinding = "if enabled:\n    t = CustomTyping" if conditional else "t = CustomTyping"
	source = (
		"import typing as t\n"
		"signature = t.overload\n"
		"class CustomTyping:\n"
		"    @staticmethod\n"
		"    def overload(function): return function\n"
		+ rebinding
		+ "\ndef target(): return 'original'\n"
		+ "@signature\ndef target(): ..."
	)
	module(bench, "api", source)
	selected = resolver(bench).resolve("erpnext.api.target")
	assert ast.literal_eval(selected.node.body[0].value) == "original"
	assert "return 'original'" in extract_function(source, "target")


@pytest.mark.parametrize(
	"projection",
	[
		"target.__kwdefaults__['option']",
		"target.__kwdefaults__.get('option')",
		"list(target.__kwdefaults__.values())[0]",
		"target.__kwdefaults__.copy()['option']",
	],
)
def test_mutating_a_shared_mutable_default_invalidates_its_owner(bench, projection):
	module(
		bench, "api", "def target(*, option=[]): return option\nalias = " + projection + "\nalias.append(2)"
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")


def test_replacing_a_slot_in_a_shallow_copy_does_not_rebind_function_defaults(bench):
	module(
		bench,
		"api",
		"def target(*, option=[]): return option\ncopy = target.__kwdefaults__.copy()\ncopy['option'] = [2]",
	)
	assert resolver(bench).resolve("erpnext.api.target").node.name == "target"


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
