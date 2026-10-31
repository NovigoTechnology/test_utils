"""Compare proven exports with Python for bounded, locally owned programs.

These snippets never import an application or modify an external module.  Python
is the oracle: a conservative refusal is acceptable, but a proven export set
must describe the program that actually ran.
"""

import ast

import pytest

from test_utils.utils.python_source import AmbiguousBinding, static_exports


def runtime_exports(source):
	namespace = {}
	exec(compile(source, "<owned-export-oracle>", "exec"), namespace)
	return set(namespace["__all__"])


def proven_exports(source):
	return static_exports(ast.parse(source).body)


def assert_sound_exports(operation):
	source = "__all__ = ['helper']\n" + operation
	expected = runtime_exports(source)
	try:
		actual = proven_exports(source)
	except AmbiguousBinding:
		return
	assert actual == expected, source


CONTAINER_PATHS = [
	("list", "box = [__all__]", "box[0]"),
	("tuple", "box = (__all__,)", "box[0]"),
	("negative-index", "box = [__all__]", "box[-1]"),
	("variable-index", "box = [__all__]\nindex = 0", "box[index]"),
	("dict", "box = {'value': __all__}", "box['value']"),
	("variable-key", "box = {'value': __all__}\nkey = 'value'", "box[key]"),
	("nested", "box = {'value': [__all__]}", "box['value'][0]"),
	("concat-left", "box = [__all__] + []", "box[0]"),
	("concat-right", "box = [] + [__all__]", "box[0]"),
	("repeat", "box = [__all__] * 1", "box[0]"),
	("slice", "box = [__all__][:]", "box[0]"),
	("step-slice", "box = [[], __all__][1::2]", "box[0]"),
	("list-constructor", "box = list([__all__])", "box[0]"),
	("tuple-constructor", "box = tuple([__all__])", "box[0]"),
	("dict-constructor", "box = dict(value=__all__)", "box['value']"),
	("dict-copy", "original = {'value': __all__}\nbox = original.copy()", "box['value']"),
	("list-unpack", "box = [*[__all__]]", "box[0]"),
	("tuple-unpack", "box = (*[__all__],)", "box[0]"),
	("dict-unpack", "box = {**{'value': __all__}}", "box['value']"),
	("assignment-unpack", "box, = [__all__]", "box"),
	("starred-assignment", "*box, = [__all__]", "box[0]"),
	("list-comprehension", "box = [target for target in [__all__]]", "box[0]"),
	("dict-comprehension", "box = {key: __all__ for key in ['value']}", "box['value']"),
	("dict-item-assignment", "box = {}\nbox['value'] = __all__", "box['value']"),
	("list-item-assignment", "box = [[]]\nbox[0] = __all__", "box[0]"),
	("list-slice-assignment", "box = []\nbox[:] = [__all__]", "box[0]"),
	("list-append", "box = []\nbox.append(__all__)", "box[0]"),
	("list-extend", "box = []\nbox.extend([__all__])", "box[0]"),
	("dict-update", "box = {}\nbox.update(value=__all__)", "box['value']"),
	("dict-union", "box = {} | {'value': __all__}", "box['value']"),
	("list-augment", "box = []\nbox += [__all__]", "box[0]"),
	("dict-augment", "box = {}\nbox |= {'value': __all__}", "box['value']"),
]


@pytest.mark.parametrize("_name, setup, target", CONTAINER_PATHS, ids=[case[0] for case in CONTAINER_PATHS])
@pytest.mark.parametrize("mutation", ["append", "slice-assignment", "augment"])
def test_nested_container_provenance_matches_python(_name, setup, target, mutation):
	if mutation == "append":
		operation = f"{target}.append('File')"
	elif mutation == "slice-assignment":
		operation = f"{target}[:] = ['File']"
	else:
		operation = f"mutated = {target}\nmutated += ['File']"
	assert_sound_exports(setup + "\n" + operation)


FLOW_CASES = [
	("conditional-true", "enabled = True\ntarget = __all__ if enabled else []"),
	("conditional-false", "enabled = False\ntarget = [] if enabled else __all__"),
	("boolean-or", "target = __all__ or []"),
	("boolean-and", "target = True and __all__"),
	("named-expression", "(target := __all__)"),
	("branch-alias", "enabled = True\nif enabled:\n    target = __all__\nelse:\n    target = []"),
	(
		"branch-nested",
		"enabled = True\nif enabled:\n    box = [__all__]\nelse:\n    box = [[]]\ntarget = box[0]",
	),
	(
		"branch-dict",
		"enabled = False\nif enabled:\n    box = {'value': []}\nelse:\n    box = {'value': __all__}\ntarget = box['value']",
	),
	(
		"loop-container",
		"box = []\nfor value in [__all__]:\n    box = [value]\ntarget = box[0]",
	),
	("try-container", "box = []\ntry:\n    box = [__all__]\nexcept ValueError:\n    pass\ntarget = box[0]"),
	(
		"finally-container",
		"box = []\ntry:\n    pass\nfinally:\n    box = [__all__]\ntarget = box[0]",
	),
]


@pytest.mark.parametrize("_name, setup", FLOW_CASES, ids=[case[0] for case in FLOW_CASES])
def test_control_flow_aliases_match_python(_name, setup):
	assert_sound_exports(setup + "\ntarget.append('File')")


CALL_CASES = [
	("positional", "def configure(target):\n    target.append('File')\nconfigure(__all__)"),
	("keyword", "def configure(target):\n    target.append('File')\nconfigure(target=__all__)"),
	("keyword-only", "def configure(*, target):\n    target.append('File')\nconfigure(target=__all__)"),
	("posonly", "def configure(target, /):\n    target.append('File')\nconfigure(__all__)"),
	("star-args", "def configure(target):\n    target.append('File')\nconfigure(*[__all__])"),
	("star-keywords", "def configure(target):\n    target.append('File')\nconfigure(**{'target': __all__})"),
	("varargs", "def configure(*args):\n    args[0].append('File')\nconfigure(__all__)"),
	("kwargs", "def configure(**kwargs):\n    kwargs['target'].append('File')\nconfigure(target=__all__)"),
	(
		"kwargs-local-collision",
		"def configure(**kwargs):\n    target = None\n    kwargs['target'].append('File')\nconfigure(target=__all__)",
	),
	(
		"posonly-keyword-collision",
		"def configure(target, /, **kwargs):\n    kwargs['target'].append('File')\nconfigure(None, target=__all__)",
	),
	("return-alias", "def identity(target):\n    return target\nidentity(__all__).append('File')"),
	("return-list", "def wrap(target):\n    return [target]\nwrap(__all__)[0].append('File')"),
	("return-dict", "def wrap(target):\n    return {'value': target}\nwrap(__all__)['value'].append('File')"),
	(
		"return-branches",
		"enabled = True\ndef wrap(target):\n    if enabled:\n        return [target]\n    else:\n        return [target]\nwrap(__all__)[0].append('File')",
	),
	(
		"return-conditional",
		"enabled = True\ndef wrap(target):\n    return [target] if enabled else [[]]\nwrap(__all__)[0].append('File')",
	),
	(
		"return-closure",
		"def factory(target):\n    def configure():\n        target.append('File')\n    return configure\nfactory(__all__)()",
	),
	(
		"closure-global",
		"def factory():\n    def configure():\n        __all__.append('File')\n    return configure\nfactory()()",
	),
	("lambda-global", "configure = lambda: __all__.append('File')\nconfigure()"),
	("lambda-argument", "configure = lambda target: target.append('File')\nconfigure(__all__)"),
	("lambda-default", "configure = lambda target=__all__: target.append('File')\nconfigure()"),
	("default", "def configure(target=__all__):\n    target.append('File')\nconfigure()"),
	("keyword-default", "def configure(*, target=__all__):\n    target.append('File')\nconfigure()"),
	(
		"nested-default",
		"def configure(box=[__all__]):\n    box[0].append('File')\nconfigure()",
	),
	(
		"default-closure",
		"def factory(target=__all__):\n    def configure():\n        target.append('File')\n    return configure\nfactory()()",
	),
	(
		"nonlocal-alias",
		"def factory():\n    target = None\n    def configure():\n        nonlocal target\n        target = __all__\n    configure()\n    target.append('File')\nfactory()",
	),
]


@pytest.mark.parametrize("_name, operation", CALL_CASES, ids=[case[0] for case in CALL_CASES])
def test_calls_and_returned_aliases_match_python(_name, operation):
	assert_sound_exports(operation)


DEFERRED_CASES = [
	("unconsumed", "pending = configure()"),
	("list", "list(configure())"),
	("tuple", "tuple(configure())"),
	("next", "next(configure())"),
	("next-keyword-default", "next(configure(), None)"),
	("for", "for value in configure():\n    pass"),
	("list-comprehension", "values = [value for value in configure()]"),
	("set-comprehension", "values = {value for value in configure()}"),
	("dict-comprehension", "values = {value: None for value in configure()}"),
	("generator-comprehension", "values = list(value for value in configure())"),
	("stored-dict", "box = dict(pending=configure())"),
	("stored-list", "box = [configure()]"),
	("stored-tuple", "box = (configure(),)"),
	("storage-then-consume", "box = dict(pending=configure())\nlist(box['pending'])"),
	("list-then-consume", "box = [configure()]\nlist(box[0])"),
	("slice-then-consume", "box = [configure()][:]\nlist(box[0])"),
	(
		"helper-keyword",
		"def consume(*, pending):\n    list(pending)\nconsume(pending=configure())",
	),
	(
		"helper-kwargs",
		"def consume(**kwargs):\n    list(kwargs['pending'])\nconsume(pending=configure())",
	),
	("helper-return", "def pending():\n    return configure()\nlist(pending())"),
	("helper-return-list", "def pending():\n    return [configure()]\nlist(pending()[0])"),
	("starred-argument", "def consume(*args):\n    pass\nconsume(*configure())"),
	("starred-list", "values = [*configure()]"),
	("starred-tuple", "values = (*configure(),)"),
]


@pytest.mark.parametrize("_name, operation", DEFERRED_CASES, ids=[case[0] for case in DEFERRED_CASES])
def test_generator_execution_boundaries_match_python(_name, operation):
	assert_sound_exports("def configure():\n    __all__.append('File')\n    yield 1\n" + operation)


YIELDED_REFERENCES = [
	("generator-for", "def refs():\n    yield __all__\nfor target in refs():\n    target.append('File')"),
	("generator-next", "def refs():\n    yield __all__\nnext(refs()).append('File')"),
	("generator-list", "def refs():\n    yield __all__\nlist(refs())[0].append('File')"),
	("genexpr-for", "for target in (value for value in [__all__]):\n    target.append('File')"),
	("genexpr-next", "next(value for value in [__all__]).append('File')"),
	("yield-from", "def refs():\n    yield from [__all__]\nfor target in refs():\n    target.append('File')"),
]


@pytest.mark.parametrize("_name, operation", YIELDED_REFERENCES, ids=[case[0] for case in YIELDED_REFERENCES])
def test_values_obtained_from_generators_match_python(_name, operation):
	assert_sound_exports(operation)


IMPLICIT_CALLS = [
	(
		"lambda-callback",
		"def invoke(callback):\n    callback()\nconfigure = lambda: __all__.append('File')\ninvoke(configure)",
	),
	(
		"decorator-factory",
		"def factory():\n    def decorate(fn):\n        __all__.append('File')\n        return fn\n    return decorate\n@factory()\ndef configure():\n    pass",
	),
	(
		"lambda-decorator",
		"decorate = lambda fn: (__all__.append('File'), fn)[1]\n@decorate\ndef configure():\n    pass",
	),
	(
		"constructor",
		"class Config:\n    def __init__(self):\n        __all__.append('File')\nConfig()",
	),
	(
		"instance-method",
		"class Config:\n    def configure(self):\n        __all__.append('File')\nConfig().configure()",
	),
	(
		"static-method",
		"class Config:\n    @staticmethod\n    def configure():\n        __all__.append('File')\nConfig.configure()",
	),
	(
		"context-manager",
		"class Config:\n    def __enter__(self):\n        __all__.append('File')\n    def __exit__(self, *args):\n        pass\nwith Config():\n    pass",
	),
	(
		"truth-test",
		"class Config:\n    def __bool__(self):\n        __all__.append('File')\n        return True\nif Config():\n    pass",
	),
	(
		"property",
		"class Config:\n    @property\n    def value(self):\n        __all__.append('File')\n        return None\nvalue = Config().value",
	),
	(
		"awaited-return",
		"async def alias():\n    return __all__\nasync def configure():\n    target = await alias()\n    target.append('File')\npending = configure()\ntry:\n    pending.send(None)\nexcept StopIteration:\n    pass",
	),
]


@pytest.mark.parametrize("_name, operation", IMPLICIT_CALLS, ids=[case[0] for case in IMPLICIT_CALLS])
def test_implicit_local_execution_matches_python(_name, operation):
	assert_sound_exports(operation)


CALLABLE_CONTAINER_PATHS = [
	("append", "box = []\nbox.append(configure)", "box[0]"),
	("extend", "box = []\nbox.extend([configure])", "box[0]"),
	("insert", "box = []\nbox.insert(0, configure)", "box[0]"),
	("update", "box = {}\nbox.update(configure=configure)", "box['configure']"),
	("setdefault", "box = {}\nbox.setdefault('configure', configure)", "box['configure']"),
	("pop", "box = [configure]", "box.pop()"),
	("get", "box = {'configure': configure}", "box.get('configure')"),
	("pop-dict", "box = {'configure': configure}", "box.pop('configure')"),
	("augment", "box = []\nbox += [configure]", "box[0]"),
]


@pytest.mark.parametrize(
	"_name, setup, callback", CALLABLE_CONTAINER_PATHS, ids=[case[0] for case in CALLABLE_CONTAINER_PATHS]
)
@pytest.mark.parametrize("lambda_callback", [False, True])
def test_inserted_and_extracted_callbacks_match_python(_name, setup, callback, lambda_callback):
	definition = (
		"configure = lambda: __all__.append('File')"
		if lambda_callback
		else "def configure():\n    __all__.append('File')"
	)
	assert_sound_exports(f"{definition}\n{setup}\n{callback}()")


SECOND_AUDIT_CASES = [
	(
		"conditional-dict-overwrite",
		"enabled = False\nbox = {'value': __all__}\nif enabled:\n    box['value'] = []\nbox['value'].append('File')",
	),
	(
		"conditional-dict-overwrite-else",
		"enabled = True\nbox = {'value': []}\nif enabled:\n    box['value'] = __all__\nelse:\n    box['value'] = []\nbox['value'].append('File')",
	),
	(
		"unknown-keyword-name",
		"key = 'target'\ndef configure(target):\n    target.append('File')\nconfigure(**{key: __all__})",
	),
	(
		"unknown-keyword-name-kwargs",
		"key = 'target'\ndef configure(**kwargs):\n    kwargs['target'].append('File')\nconfigure(**{key: __all__})",
	),
	(
		"unknown-keyword-forwarding",
		"key = 'target'\ndef configure(target):\n    target.append('File')\ndef forward(**kwargs):\n    configure(**kwargs)\nforward(**{key: __all__})",
	),
	(
		"bound-generator",
		"class Config:\n    def configure(self, target):\n        target.append('File')\n        yield 1\nlist(Config().configure(__all__))",
	),
	(
		"bound-async",
		"class Config:\n    async def configure(self, target):\n        target.append('File')\npending = Config().configure(__all__)\ntry:\n    pending.send(None)\nexcept StopIteration:\n    pass",
	),
	(
		"classmethod-async",
		"class Config:\n    @classmethod\n    async def configure(cls, target):\n        target.append('File')\npending = Config.configure(__all__)\ntry:\n    pending.send(None)\nexcept StopIteration:\n    pass",
	),
	(
		"bound-async-keyword",
		"class Config:\n    async def configure(self, *, target):\n        target.append('File')\npending = Config().configure(target=__all__)\ntry:\n    pending.send(None)\nexcept StopIteration:\n    pass",
	),
	(
		"generator-send-result",
		"def refs():\n    yield __all__\nrefs().send(None).append('File')",
	),
	(
		"generator-next-method-result",
		"def refs():\n    yield __all__\nrefs().__next__().append('File')",
	),
	(
		"generator-send-callback",
		"def configure():\n    __all__.append('File')\ndef refs():\n    yield configure\nrefs().send(None)()",
	),
	(
		"custom-await-return",
		"class AwaitAlias:\n    def __await__(self):\n        if False:\n            yield None\n        return __all__\nasync def configure():\n    target = await AwaitAlias()\n    target.append('File')\npending = configure()\ntry:\n    pending.send(None)\nexcept StopIteration:\n    pass",
	),
	(
		"distinct-cyclic-containers",
		"enabled = True\nleft = {}\nleft['self'] = left\nright = {}\nright['self'] = right\nif enabled:\n    target = left\nelse:\n    target = right",
	),
]


@pytest.mark.parametrize("_name, operation", SECOND_AUDIT_CASES, ids=[case[0] for case in SECOND_AUDIT_CASES])
def test_second_review_dataflow_cases_match_python(_name, operation):
	assert_sound_exports(operation)


PRESERVED_EXPORTS = [
	"alias = __all__",
	"box = [__all__]",
	"box = {'value': __all__}",
	"snapshot = list(__all__)\nsnapshot.append('File')",
	"snapshot = tuple(__all__)",
	"snapshot = __all__[:]\nsnapshot.append('File')",
	"def configure(target=__all__):\n    target.append('File')",
	"def configure():\n    __all__.append('File')\n    yield 1\npending = configure()",
	"pending = (__all__.append('File') for value in [0])",
	"def configure():\n    __all__.append('File')\n    yield 1\nbox = dict(value=configure())",
	"def configure():\n    __all__.append('File')\n    yield 1\nbox = [configure()]",
	"old = __all__\n__all__ = ['helper']\nold.append('File')",
	"def configure(target=__all__):\n    target.append('File')\n__all__ = ['helper']\nconfigure()",
]


@pytest.mark.parametrize("operation", PRESERVED_EXPORTS)
def test_inert_storage_and_independent_copies_keep_proven_exports(operation):
	source = "__all__ = ['helper']\n" + operation
	assert runtime_exports(source) == {"helper"}
	assert proven_exports(source) == {"helper"}
