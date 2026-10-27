import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_validate_override_annotations import (
	SHA,
	annotation,
	function_source,
	hooks,
	register_class,
	register_function,
	validate,
	write,
)
from test_validate_override_annotations import (
	bench as bench,
)

from test_utils.pre_commit import track_overrides as tracker
from test_utils.pre_commit.validate_override_annotations import main
from test_utils.utils.override_annotations import (
	AnnotationValidator,
	ResolutionError,
	SourceResolver,
	locate_app,
	read_hooks,
	validate_overrides,
)
from test_utils.utils.override_metadata import iter_annotations, parse_annotation
from test_utils.utils.python_source import extract_function, find_definition
from test_utils.utils.track_overrides import download_file_from_commit


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize(
	"mutation",
	[
		"MAPPING.update({'erpnext.accounts.utils.get_coa': 'nagus.overrides.get_coa'})",
		"MAPPING['erpnext.accounts.utils.get_coa'] = 'nagus.overrides.get_coa'",
		"configure(MAPPING)",
	],
)
def test_mutations_through_aliases_cannot_hide_registrations(bench, after, mutation):
	assignment = "override_whitelisted_methods = MAPPING\n"
	statements = assignment + mutation if after else mutation + "\n" + assignment
	path = write(bench / "nagus/nagus/hooks.py", "MAPPING = {}\n" + statements)
	with pytest.raises(ResolutionError, match=r"static values|static assignments|mutated dynamically"):
		read_hooks(path)
	assert validate(bench).errors


@pytest.mark.parametrize(
	"source",
	[
		"INNER = {}\nOUTER = [INNER]\nOUTER[0].update({'key': 'value'})\noverride_whitelisted_methods = INNER",
		"INNER = {}\noverride_whitelisted_methods = {'key': INNER}\nINNER.update({'key': 'value'})",
		"EXTENSIONS = []\nextend_doctype_class = {'Ledger': EXTENSIONS}\nEXTENSIONS.append('nagus.overrides.Mixin')",
		"APPS = []\nrequired_apps = APPS\nAPPS.append('erpnext')",
		"INNER = {}\nOUTER = {'mapping': INNER}\nALIAS = OUTER['mapping']\nALIAS.update({'key': 'value'})\noverride_whitelisted_methods = INNER",
		"INNER = {}\nALIAS, = [INNER]\nALIAS.update({'key': 'value'})\noverride_whitelisted_methods = INNER",
		"INNER = {}\ndef configure():\n    INNER.update({'key': 'value'})\nconfigure()\noverride_whitelisted_methods = INNER",
		"override_whitelisted_methods, OTHER = ({}, {})",
		"APPS = []\nrequired_apps = APPS\nAPPS += dynamic_apps()",
		"MAPPING = {}\nALIAS = MAPPING\ndel ALIAS\noverride_whitelisted_methods = ALIAS",
		"MAPPING = {}\nif condition:\n    MAPPING = {'source': 'replacement'}\noverride_whitelisted_methods = MAPPING",
	],
)
def test_nested_and_required_app_alias_mutations_are_rejected(bench, source):
	path = write(bench / "nagus/nagus/hooks.py", source)
	with pytest.raises(ResolutionError, match=r"static values|static assignments|mutated dynamically"):
		read_hooks(path)


def test_unrelated_mutated_container_does_not_invalidate_static_hooks(bench):
	path = write(
		bench / "nagus/nagus/hooks.py", "DATA = {}\nDATA.update(dynamic())\noverride_whitelisted_methods = {}"
	)
	assert read_hooks(path)["override_whitelisted_methods"] == {}


def test_overloads_do_not_mask_runtime_implementation_changes():
	source = "from typing import overload\n@overload\ndef value(arg: str) -> str: ...\n@overload\ndef value(arg: int) -> int: ...\ndef value(arg):\n    return 'before'\n"
	old = extract_function(source, "value")
	new = extract_function(source.replace("'before'", "'after'"), "value")
	assert "return 'before'" in old
	assert "@overload" not in old
	assert old != new
	assert find_definition(ast.parse(source), "value").lineno == 6


@pytest.mark.parametrize(
	"import_line,decorator",
	[
		("from typing import overload as signature", "signature"),
		("import typing as t", "t.overload"),
		("from typing_extensions import overload as signature", "signature"),
	],
)
@pytest.mark.parametrize("qualified", [False, True])
def test_overloaded_methods_resolve_implementation_with_import_aliases(import_line, decorator, qualified):
	source = f"{import_line}\nclass Controller:\n    @{decorator}\n    def validate(self, value: str) -> str: ...\n    def validate(self, value):\n        return value\n"
	method = extract_function(source, "Controller.validate" if qualified else "validate")
	assert "return value" in method
	assert decorator not in method


def test_overload_only_and_ambiguous_definitions_are_unresolved():
	assert (
		extract_function("from typing import overload\n@overload\ndef method(arg: str) -> str: ...", "method")
		is None
	)
	assert (
		extract_function(
			"if condition:\n    def method(): return 1\nelse:\n    def method(): return 2", "method"
		)
		is None
	)


def test_doctype_validation_uses_concrete_methods_with_global_overload_aliases(bench):
	write(
		bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"from typing import overload as signature\nclass Ledger:\n"
		"    @signature\n    def validate(self, value: str) -> str: ...\n"
		"    def validate(self, value):\n        return value\n",
	)
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		"from typing import overload as signature\n"
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\n"
		"class CustomLedger(Ledger):\n"
		"    @signature\n    def validate(self, value: str) -> str: ...\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def action_module():
	path = Path(__file__).resolve().parents[2] / "actions/track_overrides/track_overrides.py"
	spec = importlib.util.spec_from_file_location("review_action", path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.mark.parametrize("action", [False, True])
def test_validator_accepted_reordered_fields_are_tracked(bench, monkeypatch, action):
	fields = annotation().splitlines()
	doc = "\nProse between metadata lines.\n".join(reversed(fields))
	register_function(bench, doc)
	assert not validate(bench).errors
	module = action_module() if action else tracker
	monkeypatch.setattr(module, "get_last_commit_hash_for_file_in_branch", lambda *args: "b" * 40)
	monkeypatch.setattr(
		module,
		"download_file_from_commit",
		lambda repo, sha, path: f"def get_coa():\n    return {1 if sha == SHA else 2}\n",
	)
	if action:
		changes = module.compare_commit_hashes(str(bench / "nagus"))
	else:
		changes, unverified = module.check_tracked_methods(str(bench / "nagus"), "nvg-develop")
		assert not unverified
	assert len(changes) == 1
	assert "get_coa" in str(changes)


@pytest.mark.parametrize("comments", [False, True])
def test_tracking_parser_keeps_blocks_separate_and_supports_legacy_comments(comments):
	text = annotation()
	source = (
		"\n".join("# " + line for line in reversed(text.splitlines()))
		if comments
		else f'def custom():\n    """{text}"""\n    pass\n'
	)
	assert list(iter_annotations(source)) == [parse_annotation(text)[0]]
	assert not list(
		iter_annotations(
			'"HASH: abc"\n"REPO: https://github.com/owner/repo"\n"PATH: file.py\\nMETHOD: method"'
		)
	)


def test_adjacent_string_literals_in_a_docstring_are_tracked():
	parts = [line + "\n" for line in annotation().splitlines()]
	source = "def custom():\n    (" + "\n     ".join(repr(part) for part in reversed(parts)) + ")\n    pass\n"
	assert len(list(iter_annotations(source))) == 1


@pytest.mark.parametrize(
	"repo",
	[
		"https://github.com/Owner/repo.git/",
		"git@github.com:Owner/repo.git",
		"ssh://git@github.com/Owner/repo.git",
		"git://github.com/Owner/repo.git",
	],
)
def test_all_upstream_fetch_helpers_normalize_repository_urls(monkeypatch, repo):
	module = action_module()
	urls = []
	response = SimpleNamespace(status_code=200, text="source", json=lambda: [{"sha": "b" * 40}])
	monkeypatch.delenv("GH_TOKEN", raising=False)
	monkeypatch.delenv("GITHUB_TOKEN", raising=False)
	monkeypatch.setattr(tracker.requests, "get", lambda url, **kwargs: (urls.append(url), response)[1])
	assert download_file_from_commit(repo, SHA, "module.py") == "source"
	assert tracker.get_last_commit_hash_for_file_in_branch(repo, "module.py", "main") == "b" * 40
	assert module.get_last_commit_hash_for_file_in_branch(repo, "module.py") == "b" * 40
	assert urls == [
		f"https://raw.githubusercontent.com/owner/repo/{SHA}/module.py",
		"https://api.github.com/repos/owner/repo/commits",
		"https://api.github.com/repos/owner/repo/commits",
	]


def test_authenticated_download_normalizes_repository_url(monkeypatch):
	urls = []
	monkeypatch.setenv("GH_TOKEN", "test-token")
	monkeypatch.setattr(
		tracker.requests,
		"get",
		lambda url, **kwargs: (urls.append(url), SimpleNamespace(status_code=200, text="source"))[1],
	)
	assert download_file_from_commit("git@github.com:Owner/repo.git", SHA, "module.py") == "source"
	assert urls == ["https://api.github.com/repos/owner/repo/contents/module.py"]


@pytest.mark.parametrize("right_metadata", [False, True])
def test_extension_composition_tracks_controller_before_shared_ancestor(bench, right_metadata):
	write(
		bench / "frappe/frappe/model/document.py",
		"class Document:\n    def validate(self): return 'document'",
	)
	doc = (
		annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
		if right_metadata
		else annotation(
			method="Document.validate",
			path="frappe/model/document.py",
			repo="https://github.com/NovigoTechnology/frappe",
		)
	)
	register_class(
		bench,
		"from frappe.model.document import Document\nclass CustomLedger(Document):\n"
		+ function_source(doc, name="validate", indent="    "),
		hook="extend_doctype_class",
	)
	result = validate(bench)
	assert result.checked == 1
	assert bool(result.errors) is not right_metadata
	if not right_metadata:
		assert any("Ledger.validate" in error for error in result.errors)


def test_replacement_class_does_not_implicitly_inherit_original_controller(bench):
	write(
		bench / "frappe/frappe/model/document.py",
		"class Document:\n    def validate(self): return 'document'",
	)
	doc = annotation(
		method="Document.validate",
		path="frappe/model/document.py",
		repo="https://github.com/NovigoTechnology/frappe",
	)
	register_class(
		bench,
		"from frappe.model.document import Document\nclass CustomLedger(Document):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	assert not validate(bench).errors


def test_two_argument_validator_api_infers_extension_for_registry_callers(bench):
	write(
		bench / "frappe/frappe/model/document.py",
		"class Document:\n    def validate(self): return 'document'",
	)
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		"from frappe.model.document import Document\nclass CustomLedger(Document):\n"
		+ function_source(doc, name="validate", indent="    "),
		hook="extend_doctype_class",
	)
	apps = [locate_app(bench / name, name) for name in ("nagus", "frappe", "erpnext", "nvg_base")]
	validator = AnnotationValidator(apps[0], SourceResolver(apps))
	validator.doctype("Ledger", "nagus.overrides.CustomLedger")
	assert validator.result.checked == 1
	assert not validator.result.errors


def test_transitive_required_apps_resolve_inherited_upstream_methods(bench):
	write(
		bench / "nagus/nagus/hooks.py",
		"required_apps = ['NovigoTechnology/nvg_base']\noverride_doctype_class = {'Ledger': 'nagus.overrides.CustomLedger'}",
	)
	write(
		bench / "nvg_base/nvg_base/hooks.py",
		"required_apps = ['erpnext']\noverride_doctype_class = dynamic_unused_hook()",
	)
	write(bench / "nvg_base/nvg_base/base.py", "from erpnext.accounts.doctype.ledger.ledger import Ledger")
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	write(
		bench / "nagus/nagus/overrides.py",
		"from nvg_base.base import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	assert not validate(bench).errors


def test_additional_dependencies_enable_incomplete_required_apps(bench):
	register_function(bench, annotation())
	write(
		bench / "nagus/nagus/hooks.py",
		"override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': 'nagus.overrides.get_coa'}",
	)
	assert validate(bench).errors
	assert not validate_overrides(bench / "nagus", bench, ["NovigoTechnology/erpnext"]).errors
	assert main(["--app", str(bench / "nagus"), "--apps-root", str(bench), "--dependency", "erpnext"]) == 0
