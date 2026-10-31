"""Independent execution-oracle checks for implicit Python protocol effects."""

import ast
import sys
from contextlib import redirect_stdout
from io import StringIO
from types import ModuleType

import pytest

from test_utils.utils.python_source import AmbiguousBinding, static_exports


def assert_sound_exports(operation):
	"""Only these controlled snippets execute; application modules never do."""
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	with redirect_stdout(StringIO()):
		exec(compile(source, "<implicit-effects-oracle>", "exec"), namespace)
	actual = set(namespace["__all__"])
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		return
	assert proven == actual, "An implicit callback must not certify stale module exports"


@pytest.mark.parametrize(
	"operation",
	[
		"class Configure:\n    def __init__(self):\n        __all__.append('File')\nConfigure()",
		"class Base:\n    def __init__(self):\n        __all__.append('File')\nclass Configure(Base): pass\nConfigure()",
		"class Configure:\n    def __new__(cls):\n        __all__.append('File')\n        return object.__new__(cls)\nConfigure()",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\nbool(Configure())",
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\nlist(Configure())",
		"class Configure:\n    def __str__(self):\n        __all__.append('File')\n        return 'configuration'\nstr(Configure())",
		"class Configure:\n    def __repr__(self):\n        __all__.append('File')\n        return 'configuration'\nrepr(Configure())",
		"class Configure:\n    def __len__(self):\n        __all__.append('File')\n        return 0\nlen(Configure())",
		"class Configure:\n    def __init_subclass__(cls):\n        __all__.append('File')\nclass Child(Configure): pass",
		"class Descriptor:\n    def __get__(self, instance, owner):\n        __all__.append('File')\nclass Configure:\n    value = Descriptor()\nConfigure().value",
		"class Descriptor:\n    def __set_name__(self, owner, name):\n        __all__.append('File')\nclass Configure:\n    value = Descriptor()",
		"class Configure:\n    def __getattribute__(self, name):\n        __all__.append('File')\n        return object.__getattribute__(self, name)\n    value = None\nConfigure().value",
		"class Configure:\n    def __add__(self, other):\n        __all__.append('File')\n        return 0\nConfigure() + 1",
		"class Configure:\n    def __getitem__(self, key):\n        __all__.append('File')\n        return None\nConfigure()[0]",
		"class Configure:\n    def __contains__(self, value):\n        __all__.append('File')\n        return True\n0 in Configure()",
		"class Meta(type):\n    def __new__(mcls, name, bases, namespace):\n        __all__.append('File')\n        return type.__new__(mcls, name, bases, namespace)\nclass Configure(metaclass=Meta): pass",
		"class Configure:\n    def __enter__(self):\n        __all__.append('File')\n    def __exit__(self, *args): pass\nwith Configure(): pass",
		"class Provider:\n    def __mro_entries__(self, bases):\n        __all__.append('File')\n        return ()\nprovider = Provider()\nclass Configure(provider): pass",
		"def values():\n    __all__.append('File')\n    yield 0\ndef accept(*args): pass\naccept(*values())",
		"def values():\n    __all__.append('File')\n    yield 0\nbox = [*values()]",
		"def values():\n    __all__.append('File')\n    yield 0\nbox = (*values(),)",
		"def values():\n    __all__.append('File')\n    yield 0\nbox = {*values()}",
		"from contextlib import contextmanager\n@contextmanager\ndef context():\n    __all__.append('File')\n    yield\nwith context(): pass",
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\nif True:\n    instance = Configure()\nelse:\n    instance = Configure()\nlist(instance)",
		"class Configure:\n    def run(self, target):\n        target.append('File')\nif True:\n    run = Configure().run\nelse:\n    run = Configure().run\nrun(__all__)",
		"class Configure:\n    def __mod__(self, other):\n        __all__.append('File')\n        return 0\nConfigure() % 1",
		"class Configure:\n    def __eq__(self, other):\n        __all__.append('File')\n        return False\nConfigure() == 1",
		"class Configure:\n    def __getattr__(self, name):\n        __all__.append('File')\n        return None\nConfigure().missing",
		"class Configure:\n    def __setattr__(self, name, value):\n        __all__.append('File')\ninstance = Configure()\ninstance.value = 0",
		"class Configure:\n    def __delattr__(self, name):\n        __all__.append('File')\ninstance = Configure()\ndel instance.value",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\nwhile Configure(): pass",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\n[0 for value in [1] if Configure()]",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return False\nvalue = 1 if Configure() else 2",
		"sorted([1], key=lambda value: __all__.append('File') or value)",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nany([Configure()])",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nall([Configure()])",
		"class Configure:\n    def __hash__(self):\n        __all__.append('File')\n        return 1\nset([Configure()])",
		"class Configure:\n    def __hash__(self):\n        __all__.append('File')\n        return 1\ndict([(Configure(), 1)])",
		"class Configure:\n    def __str__(self):\n        __all__.append('File')\n        return 'configuration'\nprint(Configure())",
		"class Configure:\n    def __iter__(self):\n        __all__.append('File')\n        return iter([])\niterator = iter(Configure())",
		"class Configure:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nbox = {0: Configure()}\nrows = list(box.items())\nbool(rows[0][1])",
		"class Pure:\n    def __bool__(self): return True\nclass Dirty:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nbox = {1: Pure(), 0: Dirty()}\nrows = list(box.values())\nbool(rows[1])",
		"class Iterator:\n    def __iter__(self): return self\n    def __next__(self):\n        __all__.append('File')\n        raise StopIteration\nclass Iterable:\n    def __iter__(self): return Iterator()\nlist(Iterable())",
	],
)
def test_implicit_protocol_callbacks_cannot_certify_stale_exports(operation):
	assert_sound_exports(operation)


@pytest.mark.parametrize(
	"operation",
	[
		"class Configure: pass\ninstance = Configure()",
		"class Configure:\n    def __init__(self):\n        self.items = []\ninstance = Configure()",
		"class Configure:\n    def __bool__(self):\n        return True\nbool(Configure())",
		"class Configure:\n    def __iter__(self):\n        return iter([])\nlist(Configure())",
		"class Configure:\n    def __init_subclass__(cls):\n        pass\nclass Child(Configure): pass",
		"if True:\n    box = [None]\n    box[0] = box\nelse:\n    box = [None]\n    box[0] = box",
	],
)
def test_ordinary_class_protocols_do_not_change_actual_exports(operation):
	assert_sound_exports(operation)


@pytest.mark.parametrize("call", ["configure()", "sorted([0], key=configure)"])
def test_opaque_imported_callback_cannot_prove_unchanged_exports(monkeypatch, call):
	namespace = {}
	dependency = ModuleType("_override_audit_dependency")
	dependency.configure = lambda *args: namespace["__all__"].append("File") or 0
	monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
	source = "from _override_audit_dependency import configure\n__all__ = ['helper']\n" + call
	exec(compile(source, "<opaque-callback-oracle>", "exec"), namespace)
	assert set(namespace["__all__"]) == {"helper", "File"}
	try:
		proven = static_exports(ast.parse(source).body)
	except AmbiguousBinding:
		return
	assert proven == set(namespace["__all__"]), (
		"An opaque callback is not evidence that global effects are absent"
	)


@pytest.mark.parametrize(
	"operation",
	[
		"class Configure:\n    @property\n    def value(self):\n        __all__.append('File')\n        yield 0\npending = Configure().value",
		"class Descriptor:\n    def __get__(self, instance, owner):\n        __all__.append('File')\n        yield 0\nclass Configure:\n    value = Descriptor()\npending = Configure().value",
	],
)
def test_unconsumed_protocol_generator_has_no_export_effects(operation):
	source = "__all__ = ['helper']\n" + operation
	namespace = {}
	exec(compile(source, "<deferred-protocol-oracle>", "exec"), namespace)
	assert namespace["__all__"] == ["helper"]
	assert static_exports(ast.parse(source).body) == {"helper"}
