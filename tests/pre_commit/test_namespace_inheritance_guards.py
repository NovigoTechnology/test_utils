"""Inherited class construction and subclass hooks must not become static proofs."""

from test_controller_variants import PACKAGE, backend_annotation, controllers, mixin
from test_readonly_class_introspection import DOCUMENT, ENUMERATION, FILE
from test_track_overrides_defaults import commit
from test_track_overrides_revisions import annotate, run
from test_track_overrides_revisions import revision_bench as revision_bench
from test_validate_override_annotations import bench as bench
from test_validate_override_annotations import function_source, validate, write


def test_namespace_enumeration_rejects_values_injected_by_inherited_init_subclass(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	controller.write_text(DOCUMENT + FILE)
	baseline = commit(upstream)
	annotate(custom, baseline, ("File.save_file",))
	hostile = (
		"class Hostile:\n"
		"    @property\n"
		"    def __class__(self):\n"
		"        File.save_file.__code__ = replacement.__code__\n"
		"        return object\n"
	)
	for bases in (
		"class _DocumentBase:\n    def __init_subclass__(cls):\n        cls.sentinel = Hostile()\n",
		"class _Ancestor:\n    def __init_subclass__(cls):\n        cls.sentinel = Hostile()\n"
		"class _DocumentBase(_Ancestor): pass\n",
	):
		controller.write_text(
			hostile
			+ DOCUMENT.replace("class _DocumentBase: pass\n", bases)
			+ FILE
			+ "def replacement(self): return 'changed'\n"
			+ ENUMERATION
		)
		commit(upstream)
		assert run() == 2, bases
		output = capsys.readouterr().out
		assert "Could not check" in output
		assert "has changed" not in output


def test_controller_subclass_guard_rejects_custom_metaclass_checks_in_target_hierarchy(bench):
	meta = "class Meta(type):\n    def __subclasscheck__(cls, candidate):\n        return True\n"
	for module, owner, declaration in (
		("legacy", "LegacyLedger", "class LegacyLedger(Ledger, metaclass=Meta):\n"),
		(
			"legacy",
			"LegacyLedger",
			"class _BackendBase(Ledger, metaclass=Meta): pass\nclass LegacyLedger(_BackendBase):\n",
		),
		("modern", "ModernLedger", "class ModernLedger(Ledger, metaclass=Meta):\n"),
		("legacy", "LegacyLedger", "from abc import ABC\nclass LegacyLedger(Ledger, ABC):\n"),
		(
			"legacy",
			"LegacyLedger",
			"from abc import ABC as Abstract\nclass LegacyLedger(Ledger, Abstract):\n",
		),
	):
		controllers(bench)
		write(
			bench / "erpnext" / PACKAGE / f"{module}.py",
			"from .ledger import Ledger\n"
			+ meta
			+ declaration
			+ function_source(name="validate_duplicate_entry", indent="    "),
		)
		mixin(bench, backend_annotation())
		result = validate(bench)
		assert result.errors, (owner, declaration)
		assert any("subclass" in error.lower() or "metaclass" in error.lower() for error in result.errors), (
			owner,
			result.errors,
		)
