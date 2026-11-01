"""Resolution must prove both the selected owner and a claimed absence."""

from textwrap import indent

from test_track_overrides_defaults import commit
from test_track_overrides_revisions import annotate, run
from test_track_overrides_revisions import revision_bench as revision_bench

FILE_SOURCE = "class File:\n    def save_file(self):\n        return 'original'\n"
REPLACEMENT = FILE_SOURCE.replace("class File:", "class Replacement:").replace("'original'", "'replacement'")


def test_duplicate_owners_never_prove_a_member_deletion(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	provider = upstream / "upstream/provider.py"
	for statement, method in (
		("", "File.save_file"),
		("", "save_file"),
		("from .provider import File\n", "File.save_file"),
		("from .provider import *\n", "File.save_file"),
		("import upstream.provider as provider\n", "provider.File.save_file"),
	):
		controller.write_text(statement or FILE_SOURCE)
		provider.write_text(FILE_SOURCE)
		baseline = commit(upstream)
		annotate(custom, baseline, (method,))
		selected = provider if statement else controller
		selected.write_text(FILE_SOURCE + "class File: pass\n")
		ambiguous = commit(upstream)
		assert run() == 2, statement
		assert "has changed" not in capsys.readouterr().out
		annotate(custom, ambiguous, (method,))
		selected.write_text(FILE_SOURCE)
		commit(upstream)
		assert run() == 2, statement
		assert "has changed" not in capsys.readouterr().out
		annotate(custom, baseline, (method,))
		selected.write_text("class File: pass\n")
		commit(upstream)
		assert run() == 1, statement
		assert "return 'original'" in capsys.readouterr().out


def test_vars_writes_are_unverified_but_typing_and_deferred_calls_are_ignored(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	for source, method, target in (
		("def value():\n    return 'original'\n", "value", "value"),
		(FILE_SOURCE, "File.save_file", "File"),
	):
		source = "import builtins\nimport sys\n" + source
		controller.write_text(source)
		baseline = commit(upstream)
		annotate(custom, baseline, (method,))
		mutation = f"vars()[{target!r}] = replacement\n"
		for suffix, expected in (
			(mutation, 2),
			(f"builtins.vars()[{target!r}] = replacement\n", 2),
			(f"builtins.setattr(sys.modules[__name__], {target!r}, replacement)\n", 2),
			(f"sys.modules[__name__].__dict__[{target!r}] = replacement\n", 2),
			("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n" + indent(mutation, "    "), 0),
			("def later():\n" + indent(mutation, "    "), 0),
		):
			controller.write_text(source + suffix)
			commit(upstream)
			assert run() == expected, (method, suffix)
			output = capsys.readouterr().out
			assert "has changed" not in output
			assert ("Could not check" in output) == (expected == 2)


def test_wildcard_exports_share_namespace_write_checks(revision_bench, capsys):
	custom, upstream, baseline = revision_bench
	annotate(custom, baseline, ("File.save_file",))
	provider = upstream / "upstream/after.py"
	for exports, mutation in (
		("__all__ = []\n", "setattr(sys.modules[__name__], '__all__', ['File'])\n"),
		("__all__ = []\n", "delattr(sys.modules[__name__], '__all__')\n"),
		("__all__ = []\n", "sys.modules[__name__].__all__ = ['File']\n"),
		("__all__ = []\n", "sys.modules[__name__].__all__.append('File')\n"),
		("__all__ = []\n", "vars()['__all__'] = ['File']\n"),
		("__all__ = []\n", "builtins.vars()['__all__'] = ['File']\n"),
		("__all__ = []\n", "sys.modules[__name__].__dict__['__all__'] = ['File']\n"),
		("__all__ = []\n", "class Exporter:\n    global __all__\n    __all__ = ['File']\n"),
		("__all__ = ['File']\n", "sys.modules[__name__].__all__ = []\n"),
		("", "setattr(sys.modules[__name__], 'File', Replacement)\n"),
	):
		provider.write_text("import builtins\nimport sys\n" + exports + FILE_SOURCE + REPLACEMENT + mutation)
		commit(upstream)
		assert run() == 2, mutation
		assert "has changed" not in capsys.readouterr().out
	for mutation in (
		"setattr(sys.modules[__name__], '__all__', ['File'])\n",
		"vars()['__all__'] = ['File']\n",
		"class Exporter:\n    global __all__\n    __all__ = ['File']\n",
	):
		for guard in ("if TYPE_CHECKING:\n", "def later():\n"):
			provider.write_text(
				"import sys\nfrom typing import TYPE_CHECKING\n__all__ = []\n"
				+ FILE_SOURCE
				+ guard
				+ indent(mutation, "    ")
			)
			commit(upstream)
			assert run() == 0, (guard, mutation)
			assert "Could not check" not in capsys.readouterr().out


def test_active_class_global_bindings_are_distinct_from_local_or_deferred_bindings(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	(upstream / "upstream/replacement.py").write_text(FILE_SOURCE)
	source = FILE_SOURCE + REPLACEMENT
	controller.write_text(source)
	baseline = commit(upstream)
	annotate(custom, baseline, ("File.save_file",))
	for binding in (
		"File = Replacement\n",
		"del File\n",
		"from .replacement import File\n",
		"def File(): pass\n",
		"class File: pass\n",
	):
		controller.write_text(source + "class Mutator:\n    global File\n" + indent(binding, "    "))
		commit(upstream)
		assert run() == 2, binding
		assert "has changed" not in capsys.readouterr().out
	# A global directive is lexical even when its branch does not execute.
	controller.write_text(
		source + "from typing import TYPE_CHECKING\nclass Mutator:\n"
		"    if TYPE_CHECKING:\n        global File\n    File = Replacement\n"
	)
	commit(upstream)
	assert run() == 2
	assert "has changed" not in capsys.readouterr().out
	for suffix in (
		"class Local:\n    File = Replacement\n",
		"def later():\n    global File\n    File = Replacement\n",
		"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
		"    class Mutator:\n        global File\n        File = Replacement\n",
	):
		controller.write_text(source + suffix)
		commit(upstream)
		assert run() == 0, suffix
		assert "Could not check" not in capsys.readouterr().out


def test_class_global_getattr_keeps_unknown_module_members_unverified(revision_bench, capsys):
	custom, upstream, baseline = revision_bench
	annotate(custom, baseline, ("File.save_file",))
	controller = upstream / "upstream/controller.py"
	getter = (
		REPLACEMENT + "class Dynamic:\n    global __getattr__\n"
		"    def __getattr__(name):\n        return Replacement\n"
	)
	controller.write_text(getter)
	commit(upstream)
	assert run() == 2
	assert "has changed" not in capsys.readouterr().out
	# A module getter does not supply methods of a class that already exists.
	controller.write_text("class File: pass\n" + getter)
	commit(upstream)
	assert run() == 1
	assert "return 'original'" in capsys.readouterr().out
	controller.write_text(FILE_SOURCE + "from .after import *\n")
	for exports, expected in (("", 2), ("__all__ = ['File']\n", 2), ("__all__ = []\n", 0)):
		(upstream / "upstream/after.py").write_text(exports + getter)
		commit(upstream)
		assert run() == expected, exports
		output = capsys.readouterr().out
		assert "has changed" not in output
		assert ("Could not check" in output) == (expected == 2)
