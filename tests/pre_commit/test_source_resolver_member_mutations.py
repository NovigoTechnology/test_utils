"""Class member inspection must retain module namespace safety and import timing."""

import ast

import pytest

from test_utils.utils import override_annotations
from test_utils.utils.override_annotations import App, ResolutionError, SourceResolver

SOURCE = """class Ledger:
    def validate_duplicate_entry(self):
        return 'original'

    @classmethod
    def resolve_controller(cls):
        return cls

def replacement():
    return 'replacement'
"""
MEMBERS = ("validate_duplicate_entry", "resolve_controller")


def resolver_for(tmp_path, source):
	package = tmp_path / "upstream"
	package.mkdir(exist_ok=True)
	(package / "controller.py").write_text(source)
	return SourceResolver([App("upstream", tmp_path, package)])


def test_module_member_reassignment_rejects_stale_method_and_factory(tmp_path):
	for name in MEMBERS:
		resolver = resolver_for(tmp_path, SOURCE + f"Ledger.{name} = replacement\n")
		owner = resolver.resolve_from("upstream.controller", "Ledger")
		with pytest.raises(ResolutionError, match="Ambiguous dynamic namespace"):
			resolver.method(owner, name)


def test_class_body_member_mutation_rejects_stale_method_and_factory(tmp_path):
	for name in MEMBERS:
		resolver = resolver_for(tmp_path, SOURCE + f"class Patcher:\n    Ledger.{name} = replacement\n")
		owner = resolver.resolve_from("upstream.controller", "Ledger")
		with pytest.raises(ResolutionError, match="Ambiguous dynamic namespace"):
			resolver.method(owner, name)


def test_callable_state_mutations_are_not_treated_as_an_unchanged_method(tmp_path):
	for field in ("__code__", "__defaults__", "__kwdefaults__", "__annotations__"):
		resolver = resolver_for(
			tmp_path, SOURCE + f"Ledger.validate_duplicate_entry.{field} = replacement.{field}\n"
		)
		owner = resolver.resolve_from("upstream.controller", "Ledger")
		with pytest.raises(ResolutionError, match="Ambiguous dynamic namespace"):
			resolver.method(owner, "validate_duplicate_entry")


def test_readonly_cached_property_inventory_preserves_members(tmp_path, monkeypatch):
	proof_calls = 0
	original = override_annotations.class_values_are_safe

	def counted(*args):
		nonlocal proof_calls
		proof_calls += 1
		return original(*args)

	monkeypatch.setattr(override_annotations, "class_values_are_safe", counted)
	source = (
		"from functools import cached_property\n"
		+ SOURCE.replace(
			"class Ledger:\n",
			"class Ledger:\n    @cached_property\n    def cached(self): return 'value'\n",
		)
		+ "CACHED_PROPERTIES = tuple(\n"
		"    name for name, value in vars(Ledger).items() if isinstance(value, cached_property)\n"
		")\n"
	)
	resolver = resolver_for(tmp_path, source)
	owner = resolver.resolve_from("upstream.controller", "Ledger")
	for name in MEMBERS:
		method = resolver.method(owner, name)
		assert isinstance(method, ast.FunctionDef)
		assert method.name == name
	assert proof_calls == 1


def test_member_guard_honors_base_declaration_import_limits(tmp_path):
	for name in MEMBERS:
		resolver = resolver_for(
			tmp_path,
			SOURCE + f"class Selected(Ledger): pass\nLedger.{name} = replacement\n",
		)
		selected = resolver.resolve_from("upstream.controller", "Selected")
		owner = resolver.base_classes(selected)[0]
		assert owner.import_limits
		method = resolver.method(owner, name)
		assert isinstance(method, ast.FunctionDef)
		assert method.name == name
		# A complete-module lookup must still reject the later reassignment.
		current_owner = resolver.resolve_from("upstream.controller", "Ledger")
		with pytest.raises(ResolutionError, match="Ambiguous dynamic namespace"):
			resolver.method(current_owner, name)
