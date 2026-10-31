"""Wildcard imports must not leave stale callable definitions verifiable."""

import ast

import pytest
from test_validate_override_annotations import (
	annotation,
	register_function,
	validate,
	write,
)
from test_validate_override_annotations import bench as bench

from test_utils.pre_commit import track_overrides as tracker
from test_utils.utils.override_annotations import SourceResolver, locate_app
from test_utils.utils.python_source import extract_function, find_definition


@pytest.mark.parametrize(
	"statement,checked",
	[
		("from .active import *", 1),
		("if enabled:\n    from .active import *", 0),
		("try:\n    from .active import *\nexcept ImportError:\n    pass", 0),
	],
)
def test_wildcard_import_cannot_reuse_a_registered_functions_old_metadata(bench, statement, checked):
	path = register_function(bench, annotation())
	write(bench / "nagus/nagus/active.py", "def get_coa(): pass")
	import_line = len(path.read_text().splitlines()) + 3
	path.write_text(path.read_text() + "\n" + statement)
	result = validate(bench)
	assert result.errors
	assert result.checked == checked
	if checked:
		assert "nagus.active.get_coa" in result.errors[0]
		assert "Missing annotation fields" in result.errors[0]
	else:
		assert "ambiguous" in result.errors[0].lower()
		assert f"{path}:{import_line}" in result.errors[0]


@pytest.mark.parametrize("target", ["get_coa", "Controller.validate", "validate"])
@pytest.mark.parametrize("before", [False, True])
def test_extraction_respects_wildcard_order_and_unknown_legacy_bindings(target, before):
	definitions = "def get_coa(): pass\nclass Controller:\n    def validate(self): pass\n"
	imports = "from active import *\n"
	source = imports + definitions if before else definitions + imports
	if before and target != "validate":
		assert extract_function(source, target) is not None
		assert isinstance(find_definition(ast.parse(source), target), ast.FunctionDef)
	else:
		assert extract_function(source, target) is None
		assert find_definition(ast.parse(source), target) is None


def test_wildcard_import_replaces_an_earlier_explicit_reexport(bench):
	write(bench / "nagus/nagus/active.py", "def get_coa(): pass")
	write(
		bench / "nagus/nagus/overrides.py",
		"from erpnext.accounts.utils import get_coa\nfrom .active import *",
	)
	apps = [locate_app(bench / name, name) for name in ("nagus", "erpnext")]
	assert SourceResolver(apps).resolve("nagus.overrides.get_coa").reference == "nagus.active.get_coa"


def test_wildcard_rebinding_is_reported_by_tracking_instead_of_unchanged(bench, monkeypatch):
	register_function(bench, annotation())
	monkeypatch.setattr(tracker, "get_last_commit_hash_for_file_in_branch", lambda *args: "b" * 40)
	monkeypatch.setattr(
		tracker,
		"download_file_from_commit",
		lambda repo, sha, path: "def get_coa(): pass\nfrom active import *",
	)
	changes, unverified = tracker.check_tracked_methods(str(bench / "nagus"), "main")
	assert not unverified
	assert len(changes) == 1
	assert "could not be resolved" in changes[0]["diff"]


def test_unrelated_explicit_imports_do_not_shadow_module_functions(bench):
	path = register_function(bench, annotation())
	write(bench / "nagus/nagus/active.py", "def other(): pass")
	path.write_text(path.read_text() + "\nfrom .active import other\ndef helper():\n    return other()")
	assert not validate(bench).errors
