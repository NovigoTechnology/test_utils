"""Defaults keep object ownership across aliases, copies and rebinding."""

import pytest
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError
from test_utils.utils.python_source import extract_function


@pytest.mark.parametrize(
	"setup, mutation",
	[
		("options = []\ndef target(*, option=options): return option", "options.append(2)"),
		(
			"options = []\ndef target(*, option=options): return option",
			"def configure(value):\n    value.append(2)\nconfigure(options)",
		),
		(
			"options = {'values': []}\ndef target(option=options): return option",
			"options['values'].append(2)",
		),
		(
			"options = {'values': []}\ndef target(option=options.copy()): return option",
			"options['values'].append(2)",
		),
	],
	ids=["keyword-alias", "helper-alias", "nested-positional", "shared-child-of-copy"],
)
def test_mutation_through_an_original_alias_invalidates_the_captured_default(bench, setup, mutation):
	source = setup + "\n" + mutation
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


@pytest.mark.parametrize(
	"source",
	[
		"options = []\ndef target(option=options.copy()): return option\noptions.append(2)",
		"options = []\noptions.append(2)\ndef target(option=options): return option",
		"options = []\ndef target(option=options): return option\noptions = []\noptions.append(2)",
		"options = ([],)\ndef target(option=options): return option\noptions += (2,)",
		"options = []\ndef target(option=options): return option\nsize = len(options)",
	],
	ids=["independent-copy", "before-capture", "rebound-alias", "tuple-rebinding", "read-only"],
)
def test_operations_that_preserve_the_captured_default_keep_the_definition(bench, source):
	module(bench, "api", source)
	assert resolver(bench).resolve("erpnext.api.target").node.name == "target"
	assert extract_function(source, "target") is not None


def test_shared_default_invalidates_each_owner_without_invalidating_an_independent_function(bench):
	source = (
		"options = []\n"
		"def first(option=options): return option\n"
		"def second(*, option=options): return option\n"
		"def independent(option=[]): return option\n"
		"options.append(2)"
	)
	module(bench, "api", source)
	source_resolver = resolver(bench)
	for name in ("first", "second"):
		with pytest.raises(ResolutionError, match="Ambiguous"):
			source_resolver.resolve(f"erpnext.api.{name}")
	assert source_resolver.resolve("erpnext.api.independent").node.name == "independent"


@pytest.mark.parametrize(
	"mutation",
	[
		"from erpnext.config import options as alias\nalias.append(2)",
		"import erpnext.config\nerpnext.config.options.append(2)",
	],
	ids=["reimported-object", "module-projection"],
)
def test_imported_mutable_default_keeps_identity_across_import_forms(bench, mutation):
	module(bench, "config", "options = []")
	source = "from erpnext.config import options\ndef target(option=options): return option\n" + mutation
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None


def test_default_dictionary_lookup_with_a_callback_key_cannot_certify_old_defaults(bench):
	source = (
		"def target(*, option=[]): return option\n"
		"class Key:\n"
		"    def __hash__(self):\n"
		"        target.__kwdefaults__['option'].append(2)\n"
		"        return 0\n"
		"value = target.__kwdefaults__.get(Key())"
	)
	module(bench, "api", source)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")
	assert extract_function(source, "target") is None
