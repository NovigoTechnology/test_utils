import subprocess
import textwrap

import pytest

from test_utils.pre_commit.validate_override_annotations import main
from test_utils.utils.override_annotations import validate_overrides

SHA = "cf4e3a0715d4fcb182d645c6fca72ae345c9761f"


def write(path, source):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
	return path


def git(root, *args):
	return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def annotation(
	method="get_coa",
	path="erpnext/accounts/utils.py",
	repo="https://github.com/NovigoTechnology/erpnext",
	**fields,
):
	data = {"APP": "nagus", "HASH": SHA, "REPO": repo, "PATH": path, "METHOD": method}
	data.update(fields)
	return "\n".join(f"{key}: {value}" for key, value in data.items() if value is not None)


def function_source(doc="", *, name="get_coa", prefix="", indent=""):
	return textwrap.indent(
		f'{prefix}def {name}() -> str:\n    """{doc.replace(chr(10), chr(10) + "    ")}"""\n    return "custom"\n',
		indent,
	)


@pytest.fixture
def bench(tmp_path):
	for app in ("nagus", "frappe", "erpnext", "nvg_base"):
		root = tmp_path / app
		write(root / app / "__init__.py", "")
		git(root, "init", "-q")
		git(root, "remote", "add", "origin", f"git@github.com:NovigoTechnology/{app}.git")
	write(
		tmp_path / "frappe/frappe/model/document.py",
		"""
		class Document:
		    def on_update(self):
		        return "base"
	""",
	)
	write(
		tmp_path / "erpnext/erpnext/accounts/utils.py",
		"""
		def get_coa() -> str:
		    return "original"
		async def async_coa() -> str:
		    return "original"
	""",
	)
	write(
		tmp_path / "erpnext/erpnext/accounts/doctype/ledger/ledger.py",
		"""
		from frappe.model.document import Document
		class Ledger(Document):
		    def validate(self) -> str:
		        return "ledger"
		    async def reconcile(self) -> bool:
		        return True
	""",
	)
	write(tmp_path / "nagus/nagus/hooks.py", "required_apps = ['erpnext', 'NovigoTechnology/nvg_base']")
	return tmp_path


def hooks(bench, source):
	return write(
		bench / "nagus/nagus/hooks.py",
		"required_apps = ['erpnext', 'NovigoTechnology/nvg_base']\n" + textwrap.dedent(source),
	)


def validate(bench):
	return validate_overrides(bench / "nagus", bench)


def register_function(bench, doc="", *, prefix="", name="get_coa"):
	hooks(
		bench, f"override_whitelisted_methods = {{'erpnext.accounts.utils.{name}': 'nagus.overrides.{name}'}}"
	)
	return write(bench / "nagus/nagus/overrides.py", function_source(doc, prefix=prefix, name=name))


def register_class(bench, source, *, hook="override_doctype_class", name="CustomLedger"):
	hooks(bench, f"{hook} = {{'Ledger': ['nagus.overrides.{name}']}}")
	return write(bench / "nagus/nagus/overrides.py", source)


def test_missing_whitelisted_annotation_is_actionable(bench):
	path = register_function(bench)
	result = validate(bench)
	assert result.checked == 1
	assert len(result.errors) == 1
	assert f"{path}:1: nagus.overrides.get_coa -> erpnext.accounts.utils.get_coa" in result.errors[0]
	assert "Missing annotation fields: APP, HASH, REPO, PATH, METHOD" in result.errors[0]


@pytest.mark.parametrize("prefix,name", [("", "get_coa"), ("async ", "async_coa")])
def test_complete_function_annotation_passes_including_async_and_return_types(bench, prefix, name):
	register_function(bench, annotation(method=name), prefix=prefix, name=name)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


@pytest.mark.parametrize(
	"field,value",
	[
		("APP", "erpnext"),
		("HASH", "abc123"),
		("HASH", "g" * 40),
		("HASH", "a" * 41),
		("REPO", "https://github.com/frappe/erpnext"),
		("REPO", "not-a-repo"),
		("PATH", "erpnext/accounts/missing.py"),
		("PATH", "../erpnext/accounts/utils.py"),
		("METHOD", "other_function"),
		("METHOD", "get_coa.extra"),
	],
)
def test_invalid_annotation_fields_fail(bench, field, value):
	register_function(bench, annotation(**{field: value}))
	result = validate(bench)
	assert result.checked == 1
	assert any(field in error for error in result.errors)


@pytest.mark.parametrize("field", ["APP", "HASH", "REPO", "PATH", "METHOD"])
def test_partial_annotations_fail(bench, field):
	register_function(bench, annotation(**{field: None}))
	assert any(f"Missing annotation fields: {field}" in error for error in validate(bench).errors)


def test_annotations_must_be_in_the_overriding_docstring(bench):
	path = register_function(bench)
	path.write_text(f'"""{annotation()}"""\n' + function_source())
	assert validate(bench).errors


@pytest.mark.parametrize("hook", ["override_doctype_class", "extend_doctype_class"])
def test_doctype_methods_are_detected_and_new_helpers_ignored(bench, hook):
	register_class(
		bench,
		"""
		from erpnext.accounts.doctype.ledger.ledger import Ledger as Original
		class CustomLedger(Original):
		    def validate(self) -> str:
		        return "override"
		    async def reconcile(self) -> bool:
		        return False
		    def new_helper(self):
		        return "new"
	""",
		hook=hook,
	)
	result = validate(bench)
	assert result.checked == 2
	assert len(result.errors) == 2
	assert any(
		"CustomLedger.validate -> erpnext.accounts.doctype.ledger.ledger.Ledger.validate" in error
		for error in result.errors
	)
	assert any(
		"CustomLedger.reconcile -> erpnext.accounts.doctype.ledger.ledger.Ledger.reconcile" in error
		for error in result.errors
	)
	assert all("new_helper" not in error for error in result.errors)


def test_extension_mixin_without_explicit_upstream_base_is_detected(bench):
	register_class(
		bench,
		"""
		class CustomLedger:
		    def on_update(self):
		        pass
		    def helper(self):
		        pass
	""",
		hook="extend_doctype_class",
	)
	result = validate(bench)
	assert result.checked == 1
	assert "frappe.model.document.Document.on_update" in result.errors[0]


def test_valid_doctype_annotation_and_legacy_unique_method_pass(bench):
	doc = annotation(method="validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_import_aliases_relative_imports_and_reexports_resolve(bench):
	write(
		bench / "nagus/nagus/bases.py", "from erpnext.accounts.doctype.ledger.ledger import Ledger as Parent"
	)
	write(bench / "nagus/nagus/bridge.py", "from .bases import Parent as Base")
	register_class(
		bench,
		"""
		from . import bridge as upstream
		class CustomLedger(upstream.Base):
		    def validate(self):
		        pass
	""",
	)
	result = validate(bench)
	assert result.checked == 1
	assert "Ledger.validate" in result.errors[0]


def test_local_intermediate_classes_are_scanned(bench):
	register_class(
		bench,
		"""
		from erpnext.accounts.doctype.ledger.ledger import Ledger
		class Mixin:
		    def validate(self):
		        pass
		class CustomLedger(Mixin, Ledger):
		    pass
	""",
	)
	result = validate(bench)
	assert result.checked == 1
	assert "nagus.overrides.Mixin.validate" in result.errors[0]


def test_shadowed_local_mixin_does_not_require_annotation(bench):
	register_class(
		bench,
		"""
		from erpnext.accounts.doctype.ledger.ledger import Ledger
		class Mixin:
		    def validate(self):
		        pass
		class CustomLedger(Ledger, Mixin):
		    pass
	""",
	)
	result = validate(bench)
	assert result.checked == 0
	assert not result.errors


def test_same_named_methods_in_different_classes_resolve_correctly(bench):
	write(
		bench / "erpnext/erpnext/bases.py",
		"""
		class Wrong:
		    def validate(self):
		        return "wrong"
		class Right:
		    def validate(self):
		        return "right"
	""",
	)
	doc = annotation(method="Right.validate", path="erpnext/bases.py")
	register_class(
		bench,
		"from erpnext.bases import Right\nclass CustomLedger(Right):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	assert not validate(bench).errors
	path = bench / "nagus/nagus/overrides.py"
	path.write_text(path.read_text().replace("METHOD: Right.validate", "METHOD: Wrong.validate"))
	assert any("METHOD must identify 'Right.validate'" in error for error in validate(bench).errors)
	path.write_text(path.read_text().replace("METHOD: Wrong.validate", "METHOD: validate"))
	assert any("METHOD must identify 'Right.validate'" in error for error in validate(bench).errors)


def test_c3_multiple_inheritance_uses_actual_upstream_owner(bench):
	write(
		bench / "erpnext/erpnext/bases.py",
		"""
		class Root:
		    def validate(self):
		        return "root"
		class Left(Root):
		    pass
		class Right(Root):
		    def validate(self):
		        return "right"
		class Diamond(Left, Right):
		    pass
	""",
	)
	doc = annotation(method="Right.validate", path="erpnext/bases.py")
	register_class(
		bench,
		"from erpnext.bases import Diamond\nclass CustomLedger(Diamond):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_removed_whitelisted_upstream_method_is_reported(bench):
	register_function(bench, annotation())
	write(bench / "erpnext/erpnext/accounts/utils.py", "def renamed(): pass")
	result = validate(bench)
	assert len(result.errors) == 1
	assert "nagus.overrides.get_coa -> erpnext.accounts.utils.get_coa" in result.errors[0]
	assert "removed or renamed" in result.errors[0]


def test_removed_annotated_class_method_is_reported(bench):
	doc = annotation(method="Ledger.validate", path="erpnext/accounts/doctype/ledger/ledger.py")
	register_class(
		bench,
		"from erpnext.accounts.doctype.ledger.ledger import Ledger\nclass CustomLedger(Ledger):\n"
		+ function_source(doc, name="validate", indent="    "),
	)
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.py", "class Ledger: pass")
	result = validate(bench)
	assert len(result.errors) == 1
	assert "CustomLedger.validate" in result.errors[0]
	assert "removed or renamed" in result.errors[0]


def test_doc_events_tests_mocks_and_unrelated_functions_are_ignored(bench):
	hooks(bench, "doc_events = {'Ledger': {'validate': 'nagus.events.on_validate'}}")
	write(bench / "nagus/nagus/events.py", "def on_validate(doc, event): pass")
	write(bench / "nagus/nagus/test_overrides.py", "raise AssertionError('must not be read or imported')")
	write(bench / "nagus/nagus/unrelated.py", function_source("HASH: incomplete"))
	result = validate(bench)
	assert result.checked == 0
	assert not result.errors


def test_required_apps_both_formats_and_implicit_frappe(bench):
	write(bench / "nvg_base/nvg_base/api.py", "def original(): pass")
	write(bench / "frappe/frappe/api.py", "def original(): pass")
	hooks(
		bench,
		"""
		override_whitelisted_methods = {
		    'nvg_base.api.original': 'nagus.overrides.one',
		    'frappe.api.original': 'nagus.overrides.two',
		}
	""",
	)
	write(bench / "nagus/nagus/overrides.py", function_source(name="one") + function_source(name="two"))
	result = validate(bench)
	assert result.checked == 2
	assert "nvg_base.api.original" in result.errors[0]
	assert "frappe.api.original" in result.errors[1]


@pytest.mark.parametrize(
	"required", ["'erpnext'", "['erpnext']", "('erpnext',)", "['NovigoTechnology/erpnext']"]
)
def test_required_apps_string_list_tuple_and_owner_formats(bench, required):
	register_function(bench, annotation())
	path = bench / "nagus/nagus/hooks.py"
	path.write_text(path.read_text().replace("['erpnext', 'NovigoTechnology/nvg_base']", required))
	assert not validate(bench).errors


def test_missing_dependency_is_an_error(bench):
	git(bench / "erpnext", "remote", "remove", "origin")
	(bench / "erpnext/erpnext").rename(bench / "erpnext/missing_package")
	assert any("Cannot identify app package" in error for error in validate(bench).errors)


def test_missing_frappe_checkout_is_an_error(bench):
	(bench / "frappe").rename(bench / "not_frappe")
	assert any("App checkout not found" in error and "frappe" in error for error in validate(bench).errors)


@pytest.mark.parametrize(
	"remote",
	[
		"git@github.com:NovigoTechnology/erpnext.git",
		"ssh://git@github.com/NovigoTechnology/erpnext.git",
		"https://github.com/NovigoTechnology/erpnext.git/",
	],
)
def test_repository_remote_formats_are_equivalent(bench, remote):
	register_function(bench, annotation())
	git(bench / "erpnext", "remote", "set-url", "origin", remote)
	assert not validate(bench).errors


def test_checked_out_remote_takes_precedence_over_required_apps_owner(bench):
	register_function(bench, annotation())
	path = bench / "nagus/nagus/hooks.py"
	path.write_text(path.read_text().replace("'erpnext'", "'frappe/erpnext'"))
	assert not validate(bench).errors


def test_owner_repository_is_fallback_when_remote_is_unavailable(bench):
	register_function(bench, annotation())
	git(bench / "erpnext", "remote", "remove", "origin")
	path = bench / "nagus/nagus/hooks.py"
	path.write_text(path.read_text().replace("'erpnext'", "'NovigoTechnology/erpnext'"))
	assert not validate(bench).errors


def test_short_app_without_remote_does_not_guess_repository(bench):
	register_function(bench, annotation())
	git(bench / "erpnext", "remote", "remove", "origin")
	assert any("Cannot validate REPO" in error for error in validate(bench).errors)


def test_hooks_support_static_names_annotations_concat_and_augmented_assignments(bench):
	register_function(bench, annotation())
	write(
		bench / "nagus/nagus/hooks.py",
		"""
		BASE = ['erpnext']
		required_apps: list[str] = BASE + ['NovigoTechnology/nvg_base']
		OVERRIDES = {'erpnext.accounts.utils.get_coa': 'nagus.overrides.get_coa'}
		override_whitelisted_methods = {}
		override_whitelisted_methods |= {**OVERRIDES}
	""",
	)
	assert not validate(bench).errors


@pytest.mark.parametrize(
	"source",
	[
		"required_apps = load_dependencies()",
		"if True:\n    override_whitelisted_methods = {}",
		"override_doctype_class = {}\noverride_doctype_class.update(dynamic())",
	],
)
def test_dynamic_hooks_are_reported_instead_of_silently_skipped(bench, source):
	write(bench / "nagus/nagus/hooks.py", source)
	assert validate(bench).errors


def test_hooks_are_not_imported_and_unrelated_dynamic_settings_are_allowed(bench):
	register_function(bench, annotation())
	path = bench / "nagus/nagus/hooks.py"
	path.write_text(
		"import nonexistent_app\napp_title = nonexistent_app.get_title()\nraise RuntimeError('do not run')\n"
		+ path.read_text()
	)
	assert not validate(bench).errors


def test_invalid_hooks_syntax_and_missing_hooks_are_reported(bench):
	write(bench / "nagus/nagus/hooks.py", "required_apps = [")
	assert any("Cannot parse" in error for error in validate(bench).errors)
	(bench / "nagus/nagus/hooks.py").unlink()
	assert any("hooks.py not found" in error for error in validate(bench).errors)


@pytest.mark.parametrize(
	"source",
	[
		"required_apps = [123]",
		"required_apps = ['bad/name/entry']",
		"override_whitelisted_methods = []",
		"override_doctype_class = {'Ledger': 1}",
	],
)
def test_invalid_hook_data_is_reported(bench, source):
	hooks(bench, source)
	assert validate(bench).errors


def test_unresolved_imports_and_cycles_fail_with_context(bench):
	register_class(
		bench,
		"""
		from erpnext.missing import Ledger
		class CustomLedger(Ledger):
		    def validate(self): pass
	""",
	)
	result = validate(bench)
	assert "CustomLedger -> DocType Ledger" in result.errors[0]
	assert "erpnext.missing.Ledger" in result.errors[0]
	register_class(bench, "class CustomLedger(CustomLedger): pass")
	assert any("Cyclic inheritance" in error for error in validate(bench).errors)


def test_duplicate_annotation_fields_are_reported(bench):
	register_function(bench, annotation() + "\nAPP: nagus")
	assert any("Duplicate annotation field APP" in error for error in validate(bench).errors)


def test_cli_resolves_app_name_from_apps_root_and_returns_status(bench, monkeypatch, capsys):
	register_function(bench)
	monkeypatch.chdir(bench.parent)
	assert main(["--app", "nagus", "--apps-root", str(bench)]) == 1
	output = capsys.readouterr()
	assert "Missing annotation fields" in output.err
	assert "checked 1 override(s)" in output.out
	register_function(bench, annotation())
	assert main(["--app", str(bench / "nagus"), "--apps-root", str(bench)]) == 0


@pytest.mark.parametrize("package", [False, True])
def test_cli_defaults_work_from_repository_or_package(bench, monkeypatch, package):
	register_function(bench, annotation())
	monkeypatch.chdir(bench / "nagus/nagus" if package else bench / "nagus")
	assert main([]) == 0


def test_cli_app_name_from_current_repository_resolves_inner_package(bench, monkeypatch):
	register_function(bench, annotation())
	monkeypatch.chdir(bench / "nagus")
	assert main(["--app", "nagus", "--apps-root", ".."]) == 0


def test_static_augmented_assignments_preserve_aliases(bench):
	register_function(bench, annotation())
	write(
		bench / "nagus/nagus/hooks.py",
		"""
		BASE = []
		required_apps = BASE
		BASE += ['erpnext']
		override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': 'nagus.overrides.get_coa'}
	""",
	)
	assert not validate(bench).errors


def test_dynamic_reassignment_does_not_reuse_stale_static_values(bench):
	hooks(
		bench,
		"""
		OVERRIDES = {}
		OVERRIDES = load_overrides()
		override_whitelisted_methods = OVERRIDES
	""",
	)
	assert any("static values" in error for error in validate(bench).errors)


def test_hook_item_assignments_are_reported(bench):
	hooks(
		bench,
		"""
		override_whitelisted_methods = {}
		override_whitelisted_methods['erpnext.accounts.utils.get_coa'] = 'nagus.overrides.get_coa'
	""",
	)
	assert any("item assignment" in error for error in validate(bench).errors)


def test_invalid_dotted_hook_paths_fail(bench):
	hooks(
		bench, "override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': 'nagus../outside.function'}"
	)
	assert any("Invalid dotted Python reference" in error for error in validate(bench).errors)


def test_github_repository_names_are_case_insensitive(bench):
	register_function(bench, annotation(repo="https://github.com/novigotechnology/ERPNEXT"))
	assert not validate(bench).errors


def test_repository_with_a_different_port_fails(bench):
	register_function(bench, annotation(repo="https://github.com:8443/NovigoTechnology/erpnext"))
	assert any("REPO does not match" in error for error in validate(bench).errors)


def test_invalid_controller_metadata_is_reported_with_custom_symbol(bench):
	register_class(bench, "class CustomLedger: pass", hook="extend_doctype_class")
	write(bench / "erpnext/erpnext/accounts/doctype/ledger/ledger.json", "[]")
	result = validate(bench)
	assert "nagus.overrides.CustomLedger" in result.errors[0]
	assert "DocType metadata must be a JSON object" in result.errors[0]
