"""Only values with proven ordinary runtime types allow class namespace inventory."""

import ast

from test_utils.utils.namespace_types import class_values_are_safe
from test_utils.utils.override_annotations import App, SourceResolver


def prove(tmp_path, source, helpers=None):
	package = tmp_path / "upstream"
	package.mkdir(exist_ok=True)
	(package / "controller.py").write_text(source)
	for name, content in (helpers or {}).items():
		(package / f"{name}.py").write_text(content)
	resolver = SourceResolver([App("upstream", tmp_path, package)])
	module, _ = resolver.source_module("upstream.controller")
	owner = next(n for n in module.tree.body if isinstance(n, ast.ClassDef) and n.name == "Document")
	return class_values_are_safe(resolver, module, owner)


COLLECTION = """class Collection:
    __slots__ = ('_owner',)
    def __set_name__(self, owner, name):
        self._owner = owner

class Document:
    docs = Collection()
"""


def test_inert_local_descriptor_is_safe_and_type_hooks_are_rejected(tmp_path):
	assert prove(tmp_path, COLLECTION)
	for declaration in (
		"__class__ = property(lambda self: mutate())",
		"__getattribute__ = replacement",
		"__getattr__ = replacement",
		"def __new__(cls): return replacement",
		"def __init__(self): mutate(self)",
		"__slots__: tuple = ('__class__',)",
		"__slots__ = ('__class__',)",
	):
		assert not prove(tmp_path, COLLECTION.replace("__slots__ = ('_owner',)", declaration)), declaration
	assert not prove(tmp_path, COLLECTION.replace("self._owner = owner", "self.__class__ = owner"))
	assert not prove(
		tmp_path,
		COLLECTION.replace("docs = Collection()", "Collection = replacement\n    docs = Collection()"),
	)
	assert not prove(tmp_path, "from .helpers import Collection\nclass Document:\n    docs = Collection()\n")


def test_imported_decorator_requires_a_proven_function_return(tmp_path):
	source = "from .decorators import expose\nclass Document:\n    @expose()\n    def method(self): pass\n"
	helpers = {
		"decorators": """def expose():
    def decorate(fn):
        from .validation import validate
        fn = validate(fn)
        return fn
    return decorate
""",
		"validation": """from functools import wraps
def validate(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper
""",
	}
	assert prove(tmp_path, source, helpers)
	for returned in ("return Hostile()", "fn = Hostile()\n        return fn"):
		helpers["decorators"] = (
			"def expose():\n    def decorate(fn):\n        " + returned + "\n    return decorate\n"
		)
		assert not prove(tmp_path, source, helpers), returned


def test_standard_descriptors_require_unshadowed_names(tmp_path):
	source = """from functools import cached_property
class Document:
    @property
    def value(self): return 1
    @value.setter
    def value(self, value): pass
    @value.deleter
    def value(self): pass
    @cached_property
    def cached(self): return 1
    @staticmethod
    def plain(): pass
    @classmethod
    def create(cls): return cls
"""
	assert prove(tmp_path, source)
	assert not prove(
		tmp_path,
		source.replace("class Document:\n", "class Document:\n    def property(fn): return Hostile()\n"),
	)
	assert not prove(
		tmp_path,
		source.replace(
			"class Document:\n", "class Document:\n    def cached_property(fn): return Hostile()\n"
		),
	)
	assert not prove(
		tmp_path,
		source.replace(
			"class Document:\n",
			"class Binder:\n    global cached_property\n    cached_property = replacement\nclass Document:\n",
		),
	)


def test_decorator_parameters_and_generators_are_not_builtin_function_proofs(tmp_path):
	source = "from .decorators import expose\nclass Document:\n    @expose()\n    def method(self): return Hostile()\n"
	for callback in (
		"def decorate(property):\n        @property\n        def wrapper(): pass\n        return wrapper",
		"def decorate(functools):\n        @functools.wraps(functools)\n        def wrapper(): pass\n        return wrapper",
		"def decorate(fn):\n        @functools.wraps(fn)\n        def wrapper(): pass\n        return wrapper",
		"def decorate(fn):\n        yield fn\n        return fn",
		"def decorate(fn):\n        wraps: type\n        @wraps(fn)\n        def wrapper(): pass\n        return wrapper",
	):
		decorators = "from functools import wraps\ndef expose():\n    " + callback + "\n    return decorate\n"
		assert not prove(tmp_path, source, {"decorators": decorators}), callback
	# Function globals are looked up after the provider module finishes loading.
	assert not prove(
		tmp_path,
		source,
		{
			"decorators": """import functools
def expose():
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(): pass
        return wrapper
    return decorate
class Later:
    global functools
    functools = replacement
"""
		},
	)
	# Reassigning a property invalidates its setter/deleter proof.
	assert not prove(
		tmp_path,
		"""class Inert:
    def setter(self, fn): return Hostile()
class Document:
    @property
    def value(self): return 1
    value = Inert()
    @value.setter
    def value(self, value): pass
""",
	)
