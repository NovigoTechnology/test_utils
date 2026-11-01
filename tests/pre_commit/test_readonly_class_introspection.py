"""Read-only local class enumeration must not hide namespace mutation or rebinding."""

import ast
import textwrap
from pathlib import Path

from test_track_overrides_defaults import commit
from test_track_overrides_revisions import annotate, run
from test_track_overrides_revisions import revision_bench as revision_bench
from test_validate_override_annotations import annotation, function_source, register_class, validate, write
from test_validate_override_annotations import bench as bench

from test_utils.utils.override_annotations import App, Module, SourceResolver

DOCUMENT = """from functools import cached_property

class _DocumentBase: pass

class Document(_DocumentBase):
    @cached_property
    def cached(self):
        return 'cached'

    def on_update(self):
        return 'base'
"""
FILE = """class File(Document):
    def save_file(self):
        return 'original'
"""
ENUMERATION = """CACHED_PROPERTIES = tuple(
    prop for prop, value in vars(Document).items() if isinstance(value, cached_property)
)
"""


def test_class_namespace_read_exemption_only_applies_to_module_statements():
	source = DOCUMENT + ENUMERATION + "class File(Document): pass\n"
	tree = ast.parse(source)
	app = App("upstream", Path("/upstream"), Path("/upstream"))
	module = Module(app, "upstream.controller", app.root / "controller.py", tree)
	resolver = SourceResolver([app])
	# A declaration-time module prefix must retain the same verified read.
	assert not resolver.dynamic_namespace([tree.body[:-1]], {"File"}, tree, module=module)
	for prefix, local_import in (
		("", ""),
		("def vars(value): return {}\n", ""),
		("", "from other import Document\n"),
	):
		tree = ast.parse(
			DOCUMENT + prefix + "def factory():\n" + textwrap.indent(local_import + ENUMERATION, "    ")
		)
		factory = tree.body[-1]
		assert resolver.dynamic_namespace([factory.body], {"Document"}, tree)


def test_doctype_mro_allows_frappe_cached_property_enumeration(bench):
	write(bench / "frappe/frappe/model/document.py", DOCUMENT + ENUMERATION)
	doc = annotation(
		method="Document.on_update",
		path="frappe/model/document.py",
		repo="https://github.com/NovigoTechnology/frappe",
	)
	register_class(
		bench,
		"class CustomLedger:\n" + function_source(doc, name="on_update", indent="    "),
		hook="extend_doctype_class",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_tracker_checks_both_revisions_with_readonly_class_enumeration(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	source = DOCUMENT + FILE + ENUMERATION
	controller.write_text(source)
	baseline = commit(upstream)
	annotate(custom, baseline, ("File.save_file",))
	assert run() == 0
	assert "Could not check" not in capsys.readouterr().out
	controller.write_text(source.replace("'original'", "'updated'"))
	commit(upstream)
	assert run() == 1
	output = capsys.readouterr().out
	assert "-        return 'original'" in output
	assert "+        return 'updated'" in output
	assert "Could not check" not in output


def test_namespace_escapes_and_mutations_remain_unverified(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	source = DOCUMENT + FILE
	controller.write_text(source)
	baseline = commit(upstream)
	annotate(custom, baseline, ("File.save_file",))
	for expression in (
		"vars().items()",
		"import sys\nvars(sys.modules[__name__]).items()",
		"namespace = vars(Document)",
		"consume(vars(Document))",
		"vars(Document)['save_file'] = replacement",
		"vars(Document).update(replacement)",
		"vars(Document).pop('save_file')",
		"enumerate_later = vars(Document).items",
		"vars(Document).items(argument)",
		"vars(Document, **options).items()",
		"for name, value in vars(File).items():\n    value.__code__ = replacement.__code__",
		"for name, value in vars(File).items():\n    alias = value\n    mutate(alias)",
		"methods = tuple(value for name, value in vars(File).items())",
		"methods = tuple((name, value) for name, value in vars(File).items())",
		"methods = vars(File).items()",
		"methods = tuple(name for name, value in vars(File).items() if mutate(value))",
	):
		controller.write_text(source + expression + "\n")
		commit(upstream)
		assert run() == 2, expression
		output = capsys.readouterr().out
		assert "Could not check" in output
		assert "has changed" not in output
	# Even an items() iterator can expose mutable module export values.
	controller.write_text(source + "from .after import *\n")
	(upstream / "upstream/after.py").write_text(
		"import sys\n__all__ = []\n"
		+ FILE.replace("(Document)", "")
		+ "for name, value in vars(sys.modules[__name__]).items():\n"
		"    if name == '__all__':\n        value.append('File')\n"
	)
	commit(upstream)
	assert run() == 2
	assert "has changed" not in capsys.readouterr().out


def test_shadowed_vars_or_class_arguments_remain_unverified(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	source = DOCUMENT + FILE
	controller.write_text(source)
	baseline = commit(upstream)
	annotate(custom, baseline, ("File.save_file",))
	for selected in (
		source + "def vars(value): return {}\n" + ENUMERATION,
		source + "vars = replacement\n" + ENUMERATION,
		source + "tuple = replacement\n" + ENUMERATION,
		source + "isinstance = replacement\n" + ENUMERATION,
		source + "cached_property = replacement\n" + ENUMERATION,
		source
		+ ENUMERATION.replace("prop, value", "isinstance, value").replace("prop for", "isinstance for"),
		source + "class Binder:\n    global isinstance\n    isinstance = replacement\n" + ENUMERATION,
		source + "class Inspector:\n    vars = replacement\n    observed = vars(Document).items()\n",
		source + "[vars(Document).items() for vars in replacements]\n",
		source + "[vars(Document).items() for Document in replacements]\n",
		source + "import upstream.before as Document\n" + ENUMERATION,
		ENUMERATION + source,
		source + "class Document: pass\n" + ENUMERATION,
		source + "class Inspector:\n    Document = replacement\n    observed = vars(Document).items()\n",
		source + "class Binder:\n    global vars\n    vars = replacement\n" + ENUMERATION,
		source + "import sys\nclass Binder:\n    global Document\n"
		"    Document = sys.modules[__name__]\n" + ENUMERATION,
		source + "from .before import *\n" + ENUMERATION,
		"class Meta(type):\n    @property\n    def __dict__(cls):\n        return namespace\n"
		"class MetaBase(metaclass=Meta): pass\n"
		+ source.replace("class Document(_DocumentBase):", "class Document(MetaBase):")
		+ ENUMERATION,
	):
		controller.write_text(selected)
		commit(upstream)
		assert run() == 2, selected
		output = capsys.readouterr().out
		assert "Could not check" in output
		assert "has changed" not in output


def test_cached_property_enumeration_rejects_values_with_dynamic_type_hooks(revision_bench, capsys):
	custom, upstream, _ = revision_bench
	controller = upstream / "upstream/controller.py"
	controller.write_text(DOCUMENT + FILE)
	baseline = commit(upstream)
	annotate(custom, baseline, ("File.save_file",))
	for hook in (
		"@property\n    def __class__(self):",
		"def __getattribute__(self, name):",
	):
		hostile = (
			"class Hostile:\n    " + hook + "\n"
			"        File.save_file.__code__ = replacement.__code__\n"
			"        return object\n"
		)
		source = (
			hostile
			+ DOCUMENT.replace(
				"class Document(_DocumentBase):\n",
				"class Document(_DocumentBase):\n    sentinel = Hostile()\n",
			)
			+ FILE
			+ "def replacement(self): return 'changed'\n"
			+ ENUMERATION
		)
		controller.write_text(source)
		commit(upstream)
		assert run() == 2, hook
		assert "Could not check" in capsys.readouterr().out
	# A later write must not introduce an unverified value outside the class body.
	controller.write_text(hostile + DOCUMENT + FILE + "Document.sentinel = Hostile()\n" + ENUMERATION)
	commit(upstream)
	assert run() == 2
	assert "Could not check" in capsys.readouterr().out
