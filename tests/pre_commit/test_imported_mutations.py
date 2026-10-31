"""Importer-local mutation provenance from Copilot review 5720559061."""

import pytest
from test_validate_override_annotations import annotation, function_source, hooks, validate, write
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError


def test_imported_function_metadata_write_is_rejected_without_tainting_its_source(bench):
	module(bench, "api", "def target(): pass")
	module(bench, "bridge", "from erpnext.api import target\ntarget.__doc__ = 'replacement metadata'")
	source_resolver = resolver(bench)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.resolve("erpnext.bridge.target")
	assert source_resolver.resolve("erpnext.api.target").node.lineno == 1


def test_relative_import_metadata_write_through_a_called_helper_is_rejected(bench):
	module(bench, "api", "def target(): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndef configure(value):\n    setattr(value, '__doc__', 'changed')\nconfigure(target)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_module_import_alias_cannot_hide_a_function_code_replacement(bench):
	module(bench, "api", "def target(): pass")
	module(
		bench,
		"bridge",
		"import erpnext.api as upstream\ndef replacement(): pass\nalias = upstream.target\nalias.__code__ = replacement.__code__",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.upstream.target")


def test_wildcard_imported_function_metadata_write_is_rejected(bench):
	module(bench, "api", "__all__ = ['target']\ndef target(): pass")
	module(bench, "bridge", "from .api import *\nalias = target\nalias.__doc__ = 'changed'")
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_imported_class_member_deletion_is_not_resolved_to_the_original_method(bench):
	module(bench, "api", "class Base:\n    def target(self): pass")
	module(bench, "bridge", "from erpnext.api import Base as Parent\ndel Parent.target")
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.Parent.target")


def test_imported_class_method_metadata_write_is_rejected(bench):
	module(bench, "api", "class Base:\n    def target(self): pass")
	module(
		bench,
		"bridge",
		"from erpnext.api import Base\nalias = Base\nalias.target.__doc__ = 'changed'",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.Base.target")


def test_inheritance_cannot_erase_later_importer_side_member_mutations(bench):
	module(bench, "api", "class Base:\n    def target(self): pass")
	module(
		bench,
		"bridge",
		"from .api import Base\nclass Child(Base): pass\nBase.target = replacement",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.Child.target")


def test_importer_mutations_respect_partial_module_snapshot_and_cache_order(bench):
	module(bench, "api", "def target(): pass")
	module(bench, "bridge", "from .api import target\ntarget.__doc__ = 'changed'")
	source_resolver = resolver(bench)
	bridge = source_resolver.imported_module("erpnext.bridge")
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.qualified(bridge, "target")
	assert source_resolver.qualified(bridge, "target", before=2).reference == "erpnext.api.target"
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.qualified(bridge, "target")
	assert not any(source_resolver.import_limits.values())


def test_unexecuted_importer_mutator_and_unrelated_function_attributes_are_harmless(bench):
	module(bench, "api", "def target(): pass\ndef other(): pass")
	module(
		bench,
		"bridge",
		"from .api import target, other\ndef configure():\n    target.__doc__ = 'never executed'\nother.__doc__ = 'changed'\ntarget.debug = True",
	)
	assert resolver(bench).resolve("erpnext.bridge.target").reference == "erpnext.api.target"


def test_imported_class_mutation_does_not_replace_an_unrelated_direct_method(bench):
	module(bench, "api", "class Base:\n    def target(self): pass\n    def other(self): pass")
	module(bench, "bridge", "from .api import Base\nBase.other = replacement")
	assert resolver(bench).resolve("erpnext.bridge.Base.target").qualname == "Base.target"


def test_local_definition_after_importer_mutation_has_independent_metadata(bench):
	module(bench, "api", "def target(): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ntarget.__doc__ = 'changed'\ndef target(): return 'local'",
	)
	assert resolver(bench).resolve("erpnext.bridge.target").reference == "erpnext.bridge.target"


def test_annotation_validation_cannot_reuse_a_mutated_reexported_docstring(bench):
	hooks(
		bench,
		"override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': 'nagus.bridge.get_coa'}",
	)
	write(bench / "nagus/nagus/implementation.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/bridge.py",
		"from .implementation import get_coa\nget_coa.__doc__ = 'replacement metadata'",
	)
	assert any("Ambiguous imported binding" in error for error in validate(bench).errors)


def test_imported_keyword_defaults_alias_item_write_is_rejected(bench):
	module(bench, "api", "def target(*, option=1): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndefaults = target.__kwdefaults__\ndefaults['option'] = 2",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_imported_mutable_positional_default_changed_through_an_alias_is_rejected(bench):
	module(bench, "api", "def target(options=[]): pass")
	module(
		bench,
		"bridge",
		"from .api import target\noptions = target.__defaults__[0]\noptions.append(2)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_copying_imported_keyword_defaults_then_replacing_a_slot_is_harmless(bench):
	module(bench, "api", "def target(*, option=1): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndefaults = target.__kwdefaults__.copy()\ndefaults['option'] = 2",
	)
	assert resolver(bench).resolve("erpnext.bridge.target").reference == "erpnext.api.target"


def test_imported_keyword_defaults_copy_retains_aliases_of_nested_default_values(bench):
	module(bench, "api", "def target(*, options=[]): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndefaults = target.__kwdefaults__.copy()\ndefaults['options'].append(2)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_importer_mutation_of_the_declaring_base_affects_an_imported_child(bench):
	module(bench, "api", "class Base:\n    def target(self): pass\nclass Child(Base): pass")
	module(bench, "bridge", "from .api import Base, Child\nBase.target.__doc__ = 'changed'")
	source_resolver = resolver(bench)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.resolve("erpnext.bridge.Child.target")
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.resolve("erpnext.bridge.Child")


def test_dict_constructor_copy_retains_nested_imported_default_aliases(bench):
	module(bench, "api", "def target(*, options=[]): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndefaults = dict(target.__kwdefaults__)\ndefaults['options'].append(2)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_list_constructor_copy_retains_nested_imported_default_aliases(bench):
	module(bench, "api", "def target(options=[]): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndefaults = list(target.__defaults__)\ndefaults[0].append(2)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_replacing_a_slot_in_a_dict_constructor_copy_leaves_original_defaults_unchanged(bench):
	module(bench, "api", "def target(*, option=1): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndefaults = dict(target.__kwdefaults__)\ndefaults['option'] = 2",
	)
	assert resolver(bench).resolve("erpnext.bridge.target").reference == "erpnext.api.target"


def test_low_level_item_assignment_to_imported_keyword_defaults_is_rejected(bench):
	module(bench, "api", "def target(*, option=1): pass")
	module(bench, "bridge", "from .api import target\ndict.__setitem__(target.__kwdefaults__, 'option', 2)")
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_unknown_callback_receiving_imported_defaults_is_ambiguous(bench):
	module(bench, "api", "def target(*, option=1): pass")
	module(
		bench,
		"bridge",
		"from .api import target\ndefaults = target.__kwdefaults__\nunknown_mutator(defaults)",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")


def test_reading_imported_defaults_is_harmless(bench):
	module(bench, "api", "def target(*, option=1): pass")
	module(bench, "bridge", "from .api import target\noption = target.__kwdefaults__.get('option')")
	assert resolver(bench).resolve("erpnext.bridge.target").reference == "erpnext.api.target"


def test_wildcard_import_retains_mutations_through_other_known_exported_class_names(bench):
	module(bench, "api", "class Base:\n    def target(self): pass\nclass Child(Base): pass")
	module(bench, "bridge", "from .api import *\nBase.target.__doc__ = 'changed'")
	source_resolver = resolver(bench)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.resolve("erpnext.bridge.Child.target")
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.resolve("erpnext.bridge.Child")


def test_wildcard_mutation_of_a_different_member_does_not_replace_inherited_target(bench):
	module(
		bench,
		"api",
		"class Base:\n    def target(self): pass\n    def other(self): pass\nclass Child(Base): pass",
	)
	module(bench, "bridge", "from .api import *\nBase.other.__doc__ = 'changed'")
	assert resolver(bench).resolve("erpnext.bridge.Child.target").qualname == "Base.target"


def test_mutation_through_a_parallel_reexport_alias_reaches_the_same_defining_function(bench):
	module(bench, "api", "def target(): pass")
	module(bench, "bridge", "from .api import target")
	module(
		bench,
		"consumer",
		"from .api import target\nfrom .bridge import target as alias\nalias.__doc__ = 'changed'",
	)
	source_resolver = resolver(bench)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		source_resolver.resolve("erpnext.consumer.target")
	assert source_resolver.mutation_resolution_depth == 0
	assert source_resolver.resolve("erpnext.api.target").reference == "erpnext.api.target"


def test_imported_defaults_lookup_with_a_custom_key_keeps_possible_hash_side_effects(bench):
	module(bench, "api", "def target(*, option=[]): pass")
	module(
		bench,
		"bridge",
		"from .api import target\nclass Key:\n    def __hash__(self):\n        target.__kwdefaults__['option'].append(2)\n        return 123\nvalue = target.__kwdefaults__.get(Key())",
	)
	with pytest.raises(ResolutionError, match="Ambiguous imported binding"):
		resolver(bench).resolve("erpnext.bridge.target")
