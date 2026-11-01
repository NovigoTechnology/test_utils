"""Track wildcard-bound methods using the sources of each compared Git revision."""

from types import SimpleNamespace

import pytest
from test_track_overrides_defaults import commit, git, repository

from test_utils.pre_commit import track_overrides as tracker
from test_utils.utils.override_annotations import AnnotationValidator, SourceResolver, locate_app

REPO = "https://github.com/example/upstream"
METHODS = ("File.save_file", "File.handle_is_private_changed")
SOURCE = """from .before import *

class File:
    def save_file(self):
        return 'original'

    def handle_is_private_changed(self):
        return 'private'

from .after import *
"""


def annotate(custom, sha, methods=METHODS, *, path="upstream/controller.py"):
	(custom / "custom/override.py").write_text(
		"\n".join(
			f'''def custom_{index}():
    """
    APP: custom
    HASH: {sha}
    REPO: {REPO}
    PATH: {path}
    METHOD: {method}
    """
    pass
'''
			for index, method in enumerate(methods)
		)
	)


@pytest.fixture
def revision_bench(tmp_path, monkeypatch):
	custom = repository(tmp_path / "custom", "https://github.com/example/custom")
	upstream = repository(tmp_path / "dependency", REPO)
	for root, name in ((custom, "custom"), (upstream, "upstream")):
		(root / name).mkdir()
		(root / name / "__init__.py").write_text("")
		(root / name / "hooks.py").write_text("")
	for provider in ("before", "after"):
		(upstream / f"upstream/{provider}.py").write_text(
			"__all__ = []\nraise AssertionError('Analysis must never execute this module')\n"
		)
	(upstream / "upstream/controller.py").write_text(SOURCE)
	sha = commit(upstream)
	annotate(custom, sha)
	commit(custom)
	monkeypatch.chdir(custom)
	monkeypatch.setattr(
		tracker.requests, "get", lambda *args, **kwargs: pytest.fail("Unexpected network request")
	)
	return custom, upstream, sha


def run(arguments=()):
	with pytest.raises(SystemExit) as result:
		tracker.main(["--no-color", *arguments])
	return result.value.code


def test_tracker_and_annotation_validator_resolve_the_same_file_methods(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	app = locate_app(custom)
	resolver = SourceResolver([app, locate_app(upstream, "upstream")])
	validator = AnnotationValidator(app, resolver)
	for index, method in enumerate(METHODS):
		original = resolver.resolve_from("upstream.controller", method)
		replacement = resolver.resolve_from("custom.override", f"custom_{index}")
		validator.validate_annotation(replacement, original)
	assert validator.result.checked == 2
	assert not validator.result.errors
	assert run() == 0
	assert "has changed" not in capsys.readouterr().out


def test_resolved_wildcard_method_changes_produce_a_diff(revision_bench, capsys):
	_, upstream, _ = revision_bench
	(upstream / "upstream/controller.py").write_text(SOURCE.replace("'original'", "'updated'"))
	commit(upstream)
	assert run() == 1
	output = capsys.readouterr().out
	assert "File.save_file" in output
	assert "-        return 'original'" in output
	assert "+        return 'updated'" in output
	assert "File.handle_is_private_changed` in" not in output


@pytest.mark.parametrize("remote", [False, True])
def test_changed_exports_use_their_own_revision_even_when_controller_is_unchanged(
	revision_bench, capsys, monkeypatch, remote
):
	custom, upstream, sha = revision_bench
	annotate(custom, sha, ("File.save_file",))
	(upstream / "upstream/after.py").write_text(
		"__all__ = ['File']\nclass File:\n    def save_file(self):\n        return 'replacement'\n"
	)
	target_sha = commit(upstream)
	trees = set()
	ref_requests = []
	if remote:

		def get(url, **kwargs):
			if url.endswith("/commits"):
				ref_requests.append(kwargs["params"])
				return SimpleNamespace(status_code=200, json=lambda: [{"sha": target_sha}])
			assert "/git/trees/" in url
			revision = url.rsplit("/", 1)[1]
			trees.add(revision)
			paths = git(upstream, "ls-tree", "-r", "--name-only", revision).splitlines()
			return SimpleNamespace(
				status_code=200,
				json=lambda: {
					"truncated": False,
					"tree": [{"type": "blob", "path": path} for path in paths],
				},
			)

		monkeypatch.setattr(tracker.requests, "get", get)
		monkeypatch.setattr(
			tracker,
			"download_file_from_commit",
			lambda repo, revision, path: git(upstream, "show", f"{revision}:{path}"),
		)
	assert (upstream / "upstream/controller.py").read_text() == SOURCE
	assert run(["--base-branch", "develop"] if remote else []) == 1
	output = capsys.readouterr().out
	assert "-        return 'original'" in output
	assert "+        return 'replacement'" in output
	if remote:
		assert ref_requests == [{"sha": "develop", "per_page": 1}]
		assert trees == {sha, target_sha}


@pytest.mark.parametrize("side", ["baseline", "target"])
def test_ambiguous_exports_are_unverified_in_either_revision(revision_bench, capsys, side):
	custom, upstream, _ = revision_bench
	provider = upstream / "upstream/after.py"
	provider.write_text("__all__ = discover_exports()\n")
	ambiguous_sha = commit(upstream)
	if side == "baseline":
		annotate(custom, ambiguous_sha)
		provider.write_text("__all__ = []\n")
		commit(upstream)
	assert run() == 2
	output = capsys.readouterr().out
	assert "has changed" not in output
	assert "Could not check" in output
	assert "__all__" in output


@pytest.mark.parametrize("deletion", ["method", "file"])
def test_verified_wildcard_methods_still_report_confirmed_deletion(revision_bench, capsys, deletion):
	custom, upstream, sha = revision_bench
	annotate(custom, sha, ("File.save_file",))
	controller = upstream / "upstream/controller.py"
	if deletion == "file":
		controller.unlink()
	else:
		controller.write_text(SOURCE.replace("    def save_file(self):\n        return 'original'\n", ""))
	commit(upstream)
	assert run() == 1
	output = capsys.readouterr().out
	assert "has changed" in output
	assert "-        return 'original'" in output


def test_identical_sources_do_not_validate_an_inexistent_method(revision_bench, capsys):
	custom, _, sha = revision_bench
	annotate(custom, sha, ("File.missing",))
	assert run() == 2
	output = capsys.readouterr().out
	assert "File.missing" in output
	assert "has changed" not in output


def test_mixed_changed_and_unverified_results_keep_both_diagnostics_and_exit_two(revision_bench, capsys):
	custom, upstream, sha = revision_bench
	annotate(custom, sha, ("File.save_file", "File.missing"))
	(upstream / "upstream/controller.py").write_text(SOURCE.replace("'original'", "'updated'"))
	commit(upstream)
	assert run() == 2
	output = capsys.readouterr().out
	assert "File.save_file` in `upstream/controller.py` has changed" in output
	assert "Could not check" in output
	assert "File.missing" in output


def test_legacy_leaf_method_names_remain_supported_when_unique(revision_bench, capsys):
	custom, _, sha = revision_bench
	annotate(custom, sha, ("save_file",))
	assert run() == 0
	assert "has changed" not in capsys.readouterr().out


@pytest.mark.parametrize("deletion,exit_code", [("method", 1), ("import_target", 2)])
def test_missing_imported_class_members_are_distinct_from_broken_imports(
	revision_bench, capsys, deletion, exit_code
):
	custom, upstream, _ = revision_bench
	(upstream / "upstream/controller.py").write_text("from .provider import File\n")
	provider = upstream / "upstream/provider.py"
	provider.write_text("class File:\n    def save_file(self):\n        return 'original'\n")
	sha = commit(upstream)
	annotate(custom, sha, ("File.save_file",))
	provider.write_text("class File: pass\n" if deletion == "method" else "class Other: pass\n")
	commit(upstream)
	assert run() == exit_code
	output = capsys.readouterr().out
	if deletion == "method":
		assert "has changed" in output
		assert "-        return 'original'" in output
	else:
		assert "has changed" not in output
		assert "Ambiguous import" in output


def test_legacy_leaf_method_with_overloads_resolves_its_single_implementation(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	(upstream / "upstream/controller.py").write_text(
		"from typing import overload\nclass File:\n"
		"    @overload\n    def save_file(self, value: int) -> int: ...\n"
		"    @overload\n    def save_file(self, value: str) -> str: ...\n"
		"    def save_file(self, value):\n        return value\n"
	)
	sha = commit(upstream)
	annotate(custom, sha, ("save_file",))
	assert run() == 0
	assert "has changed" not in capsys.readouterr().out


def test_dynamic_namespaces_do_not_prove_symbol_deletion(revision_bench, capsys):
	custom, upstream, sha = revision_bench
	annotate(custom, sha, ("File.save_file",))
	for source in (
		"def __getattr__(name):\n    return object()\n",
		"exec('class File: pass')\n",
		"globals()['File'] = build_file()\n",
	):
		(upstream / "upstream/controller.py").write_text(source)
		commit(upstream)
		assert run() == 2
		output = capsys.readouterr().out
		assert "has changed" not in output
		assert "dynamic namespace" in output


def test_explicit_package_path_does_not_select_a_colliding_module(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	(upstream / "upstream/collision.py").write_text("def value():\n    return 'decoy'\n")
	package = upstream / "upstream/collision"
	package.mkdir()
	path = package / "__init__.py"
	path.write_text("def value():\n    return 'before'\n")
	sha = commit(upstream)
	annotate(custom, sha, ("value",), path="upstream/collision/__init__.py")
	path.write_text("def value():\n    return 'after'\n")
	commit(upstream)
	assert run() == 1
	output = capsys.readouterr().out
	assert "-    return 'before'" in output
	assert "+    return 'after'" in output
	assert "decoy" not in output


def test_duplicate_owners_are_unverified_for_qualified_legacy_and_reexported_methods(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	provider = upstream / "upstream/provider.py"
	provider.write_text(
		"class Base:\n    def validate(self):\n        return 'first'\n"
		"class Controller(Base): pass\n"
		"class Base:\n    def validate(self):\n        return 'second'\n"
	)
	(upstream / "upstream/controller.py").write_text("from .provider import Base\n")
	sha = commit(upstream)
	provider.write_text(provider.read_text().replace("'first'", "'changed'"))
	commit(upstream)
	for path, method in (
		("upstream/provider.py", "Base.validate"),
		("upstream/provider.py", "validate"),
		("upstream/controller.py", "Base.validate"),
	):
		annotate(custom, sha, (method,), path=path)
		assert run() == 2
		output = capsys.readouterr().out
		assert "has changed" not in output
		assert "Ambiguous" in output


def test_wildcards_can_resolve_other_module_roots_from_the_same_revision(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	(upstream / "helpers.py").write_text("__all__ = []\n")
	(upstream / "support").mkdir()
	(upstream / "support/exports.py").write_text("__all__ = []\n")
	controller = upstream / "upstream/controller.py"
	controller.write_text(SOURCE + "from helpers import *\nfrom support.exports import *\n")
	sha = commit(upstream)
	annotate(custom, sha, ("File.save_file",))
	assert run() == 0
	assert "Could not check" not in capsys.readouterr().out
	(upstream / "helpers.py").write_text(
		"__all__ = ['File']\nclass File:\n    def save_file(self):\n        return 'replacement'\n"
	)
	commit(upstream)
	assert run() == 1
	assert "+        return 'replacement'" in capsys.readouterr().out


def test_enclosing_mutations_do_not_prove_class_member_deletion(revision_bench, capsys):
	custom, upstream, sha = revision_bench
	annotate(custom, sha, ("File.save_file",))
	for mutation in (
		"File.save_file = replacement\n",
		"Alias = File\nAlias.save_file = replacement\n",
		"if enabled:\n    File.save_file = replacement\n",
		"setattr(File, 'save_file', replacement)\n",
		"exec('File.save_file = replacement')\n",
	):
		(upstream / "upstream/controller.py").write_text("class File: pass\n" + mutation)
		commit(upstream)
		assert run() == 2
		output = capsys.readouterr().out
		assert "has changed" not in output
		assert "dynamic" in output.lower()


def test_typing_only_and_deferred_writes_do_not_make_runtime_methods_ambiguous(revision_bench, capsys):
	_, upstream, _ = revision_bench
	for source in (
		SOURCE.replace(
			"class File:\n",
			"from typing import TYPE_CHECKING\nclass File:\n    if TYPE_CHECKING:\n        exec('pass')\n",
		),
		"from typing import TYPE_CHECKING\nclass Other:\n"
		"    if TYPE_CHECKING:\n        exec('pass')\n" + SOURCE,
		SOURCE + "def later():\n    File.save_file = replacement\n",
		SOURCE
		+ "import sys\nfrom typing import TYPE_CHECKING\n"
		+ "if TYPE_CHECKING:\n    sys.modules[__name__].File = Replacement\n",
		"import sys\n" + SOURCE + "def later():\n    sys.modules[__name__].File = Replacement\n",
	):
		(upstream / "upstream/controller.py").write_text(source)
		commit(upstream)
		assert run() == 0
		assert "Could not check" not in capsys.readouterr().out


def test_dynamic_writes_do_not_verify_a_stale_method_body(revision_bench, capsys):
	_, upstream, _ = revision_bench
	(upstream / "upstream/controller.py").write_text(SOURCE + "File.save_file = replacement\n")
	commit(upstream)
	assert run() == 2
	output = capsys.readouterr().out
	assert "has changed" not in output
	assert "dynamic" in output.lower()


def test_dynamic_module_writes_do_not_verify_existing_functions_or_reexports(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	provider = upstream / "upstream/provider.py"
	source = "def value():\n    return 'original'\n"
	provider.write_text(source)
	(upstream / "upstream/controller.py").write_text("from .provider import value\n")
	sha = commit(upstream)
	for mutation in ("exec('pass')\n", "globals()['value'] = replacement\n"):
		provider.write_text(source + mutation)
		commit(upstream)
		for path in ("upstream/provider.py", "upstream/controller.py"):
			annotate(custom, sha, ("value",), path=path)
			assert run() == 2
			output = capsys.readouterr().out
			assert "has changed" not in output
			assert "dynamic" in output.lower()


def test_module_attribute_writes_do_not_verify_replaced_class_owners(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	(upstream / "upstream/provider.py").write_text(SOURCE)
	for source, method, owner in (
		(SOURCE, "File.save_file", "File"),
		("import upstream.provider as provider\n", "provider.File.save_file", "provider.File"),
		(
			"class Outer:\n    class File:\n        def save_file(self):\n            return 'original'\n",
			"Outer.File.save_file",
			"Outer.File",
		),
	):
		controller = upstream / "upstream/controller.py"
		controller.write_text("import sys\n" + source)
		sha = commit(upstream)
		annotate(custom, sha, (method,))
		controller.write_text(controller.read_text() + f"sys.modules[__name__].{owner} = Replacement\n")
		commit(upstream)
		assert run() == 2, method
		output = capsys.readouterr().out
		assert "has changed" not in output
		assert "dynamic" in output.lower()


def test_imported_module_members_are_distinct_from_missing_modules_and_dangling_from_imports(
	revision_bench, capsys
):
	custom, upstream, _ = revision_bench
	function = "def value():\n    return 'original'\n"
	file_class = "class File:\n    def save_file(self):\n        return 'original'\n"
	provider = upstream / "upstream/provider.py"
	for statement, method, source, removed, expected, module_missing in (
		("import upstream.provider as provider", "provider.value", function, "pass\n", 1, 2),
		("import upstream.provider", "upstream.provider.value", function, "pass\n", 1, 2),
		("from upstream import provider", "provider.value", function, "pass\n", 1, 2),
		("import upstream as root", "root.provider.value", function, "pass\n", 1, 1),
		(
			"import upstream.provider as provider",
			"provider.File.save_file",
			file_class,
			"class File: pass\n",
			1,
			2,
		),
		("import upstream.provider as provider", "provider.File.save_file", file_class, "pass\n", 1, 2),
		("import upstream as root", "root.provider.File.save_file", file_class, "class File: pass\n", 1, 1),
		("from .provider import value", "value", function, "pass\n", 2, 2),
		("from .provider import File", "File.save_file", file_class, "pass\n", 2, 2),
	):
		(upstream / "upstream/controller.py").write_text(statement + "\n")
		provider.write_text(source)
		sha = commit(upstream)
		annotate(custom, sha, (method,))
		provider.write_text(removed)
		commit(upstream)
		assert run() == expected, method
		output = capsys.readouterr().out
		if expected == 1:
			assert "has changed" in output
			assert "return 'original'" in output
		else:
			assert "has changed" not in output
			assert "Could not check" in output
		provider.unlink()
		commit(upstream)
		assert run() == module_missing, method
		assert ("has changed" in capsys.readouterr().out) == (module_missing == 1)


def test_wildcard_export_checks_ignore_typing_only_class_effects_but_reject_active_effects(
	revision_bench, capsys
):
	_, upstream, _ = revision_bench
	for effect in ("exec('pass')", "__all__.append('File')"):
		for typing_only, expected in ((True, 0), (False, 2)):
			body = f"    if TYPE_CHECKING:\n        {effect}\n" if typing_only else f"    {effect}\n"
			(upstream / "upstream/after.py").write_text(
				"from typing import TYPE_CHECKING\n__all__ = []\nclass Other:\n" + body
			)
			commit(upstream)
			assert run() == expected, (effect, typing_only)
			output = capsys.readouterr().out
			assert "has changed" not in output
			assert ("Could not check" in output) == (expected == 2)
