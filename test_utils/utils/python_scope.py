"""Track lexical provenance and executed effects without running application code."""

import ast
from copy import copy
from dataclasses import dataclass, replace

from test_utils.utils.python_ast import (
	DEFINITION_NODES,
	FUNCTION_NODES,
	definition_expressions,
	scope_bindings,
	scope_statements,
)


@dataclass
class SourceValue:
	"""Provenance of a value; expressions retain references to module containers."""

	expression: ast.expr
	modules: frozenset[str] = frozenset()
	helpers: tuple = ()
	is_class: bool = False
	items: dict | None = None
	bound: tuple = ()
	instance: bool = False
	scalar: bool = False
	key_values: dict | None = None
	view: tuple | None = None
	origins: frozenset = frozenset()
	deferred: tuple = ()
	false_constant: bool = False
	uncertain: bool = False


@dataclass(eq=False)
class SourceFunction:
	node: ast.AST
	closure: object
	defaults: dict
	deferred: bool = False
	descriptor: str | None = None


@dataclass(eq=False)
class DeferredExecution:
	helper: SourceFunction | None
	arguments: list
	keywords: dict
	expression: ast.GeneratorExp | None = None
	scope: object = None
	outer_iterable: SourceValue | None = None
	advance: SourceValue | None = None


@dataclass
class SourceEffect:
	node: ast.AST
	bindings: frozenset[str] = frozenset()
	namespace: bool = False
	typing_modules: frozenset[str] = frozenset()
	replaced_definition: str | None = None
	mutation: bool = False
	shallow: bool = False
	opaque: bool = False
	origins: frozenset = frozenset()


class SourceScope:
	def __init__(self, parent=None, *, kind="module", body=()):
		self.parent = parent
		self.kind = kind
		self.root = parent.root if parent else self
		self.values = {}
		self.globals = set()
		self.nonlocals = set()
		for statement in scope_statements(body):
			if isinstance(statement, ast.Global):
				self.globals.update(statement.names)
			elif isinstance(statement, ast.Nonlocal):
				self.nonlocals.update(statement.names)
		if kind == "function":
			names = set(scope_bindings(body))
			names.update(
				item.target.id
				for item in scope_statements(body)
				if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
			)
			self.values.update(
				{name: SourceValue(ast.Constant(None)) for name in names - self.globals - self.nonlocals}
			)

	def binding_scope(self, name):
		if name in self.globals:
			return self.root
		if name in self.nonlocals:
			owner = self.parent
			while owner.parent and name not in owner.values:
				owner = owner.parent
			return owner
		return self

	def get(self, name):
		owner = self.binding_scope(name)
		if name in owner.values:
			return owner.values[name]
		parent = owner.parent
		while parent and parent.kind == "class":
			parent = parent.parent
		if parent:
			return parent.get(name)
		return SourceValue(ast.Name(id=name, ctx=ast.Load()))

	def set(self, name, value, *, anchor=True):
		owner = self.binding_scope(name)
		if owner is owner.root and anchor:
			value = replace(value, expression=ast.Name(id=name, ctx=ast.Load()))
		owner.values[name] = value
		return owner is self.root and self is not self.root


class EffectAnalyzer:
	"""Conservative lexical analysis of executed syntax, never importing an app."""

	ASYNC_ADVANCES = ("__anext__", "asend", "athrow")  # codespell:ignore asend

	BUILTINS = (
		"vars",
		"globals",
		"locals",
		"exec",
		"eval",
		"setattr",
		"delattr",
		"list",
		"tuple",
		"set",
		"dict",
		"next",
		"sum",
		"any",
		"all",
		"sorted",
		"iter",
		"print",
		"id",
		"type",
		"repr",
		"str",
		"bool",
		"len",
		"frozenset",
		"hash",
		"format",
		"staticmethod",
		"classmethod",
		"property",
	)

	def __init__(self):
		self.scope = SourceScope()
		for name in self.BUILTINS:
			self.scope.values[name] = SourceValue(
				ast.Name(id=name, ctx=ast.Load()), modules=frozenset({f"builtins.{name}"})
			)
		self.effects = []
		self.call_values = {}
		self.invalid_definitions = set()
		self.runtime_bodies = {}
		self.changed_typing = set()
		self.module_writes = {}
		self.return_values = []
		self.yield_values = []
		self.member_writes = {}
		self.branch_depth = 0

	def emit(self, effect):
		self.effects.append(effect)
		self.changed_typing.update(effect.typing_modules)
		if effect.namespace:
			self.changed_typing.update({"typing", "typing_extensions"})

	@staticmethod
	def reachable(value, seen=frozenset()):
		if id(value) in seen:
			return frozenset()
		return value.origins | frozenset().union(
			*(EffectAnalyzer.reachable(item, seen | {id(value)}) for item in (value.items or {}).values())
		)

	@staticmethod
	def reachable_modules(value, seen=frozenset()):
		if id(value) in seen:
			return frozenset()
		return value.modules | frozenset().union(
			*(
				EffectAnalyzer.reachable_modules(item, seen | {id(value)})
				for item in [*(value.items or {}).values(), *value.bound]
			)
		)

	def value(self, node, scope):
		if node in self.call_values:
			return self.call_values[node]
		if isinstance(node, ast.Name):
			return self.project_view(scope.get(node.id))
		if isinstance(node, ast.Constant):
			return SourceValue(node, scalar=True)
		if isinstance(node, ast.Attribute):
			base = self.value(node.value, scope)
			if base.items is not None and node.attr in base.items and (base.is_class or base.instance):
				member = base.items[node.attr]
				bound = (
					(base,)
					if any(
						helper.descriptor == "class" or (base.instance and helper.descriptor != "static")
						for helper in member.helpers
					)
					else ()
				)
				return replace(member, bound=bound)
			modules = frozenset(f"{name}.{node.attr}" for name in base.modules)
			return SourceValue(
				ast.copy_location(ast.Attribute(value=base.expression, attr=node.attr, ctx=node.ctx), node),
				modules=modules,
				origins=frozenset()
				if node.attr in {"__name__", "__qualname__", "__module__"}
				and (
					base.is_class
					or base.helpers
					or any(isinstance(origin, DEFINITION_NODES) for origin in base.origins)
				)
				else self.reachable(base),
				false_constant=node.attr == "TYPE_CHECKING"
				and bool(base.modules & {"typing", "typing_extensions"})
				and not base.uncertain
				and not (base.modules & self.changed_typing),
			)
		if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
			values = []
			for item in node.elts:
				value = self.value(item, scope)
				values.extend(
					value.items.values()
					if isinstance(item, ast.Starred) and value.items is not None
					else [value]
				)
			items = dict(enumerate(values))
			expression = ast.Tuple(elts=[value.expression for value in values], ctx=ast.Load())
			return SourceValue(ast.copy_location(expression, node), items=items, origins=frozenset({node}))
		if isinstance(node, ast.Dict):
			items = {}
			key_values = {}
			possible = []
			for key, item in zip(node.keys, node.values, strict=True):
				value = self.value(item, scope)
				if key is None:
					items.update(value.items or {})
					key_values.update(value.key_values or {})
					possible.append(value)
				else:
					try:
						index = ast.literal_eval(key)
						items[index] = value
						key_values[index] = self.value(key, scope)
					except ValueError, TypeError:
						items[None] = self.merge([items[None], value]) if None in items else value
						key_values[None] = self.value(key, scope)
						possible.append(value)
			return SourceValue(
				node,
				items=items,
				key_values=key_values,
				origins=frozenset({node}) | frozenset().union(*(self.reachable(value) for value in possible)),
			)
		if isinstance(node, ast.NamedExpr):
			return self.value(node.value, scope)
		if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
			base = self.value(node.value, scope)
			try:
				if isinstance(node.slice, ast.Slice) and base.items is not None:
					index = slice(
						*(
							ast.literal_eval(item) if item is not None else None
							for item in (node.slice.lower, node.slice.upper, node.slice.step)
						)
					)
					selected = list(base.items.values())[index]
					return SourceValue(node, items=dict(enumerate(selected)), origins=frozenset({node}))
				index = ast.literal_eval(node.slice)
				if base.items is not None and index in base.items:
					return (
						self.merge([base.items[index], base.items[None]])
						if None in base.items and index is not None
						else base.items[index]
					)
			except ValueError, TypeError:
				pass
			possible = list((base.items or {}).values())
			result = self.merge(possible) if possible else SourceValue(node)
			return replace(result, origins=result.origins | self.reachable(base), uncertain=True)
		if not isinstance(node, ast.expr):
			return SourceValue(ast.Constant(None))
		expression = copy(node)
		for key, value in ast.iter_fields(node):
			if isinstance(value, ast.expr):
				setattr(expression, key, self.value(value, scope).expression)
			elif isinstance(value, list):
				setattr(
					expression,
					key,
					[
						self.value(item, scope).expression
						if isinstance(item, ast.expr)
						else ast.keyword(arg=item.arg, value=self.value(item.value, scope).expression)
						if isinstance(item, ast.keyword)
						else item
						for item in value
					],
				)
		operands = [
			self.value(child, scope) for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)
		]
		possible = self.merge(operands) if operands else SourceValue(expression)
		return replace(
			possible,
			expression=expression,
			origins=frozenset().union(*(self.reachable(value) for value in operands)),
			uncertain=True,
		)

	def scan(self, statement):
		self.effects = []
		self.visit(statement, self.scope, frozenset())
		return self.effects

	def expression(self, node, scope, seen):
		if isinstance(node, (ast.Yield, ast.YieldFrom)):
			if node.value is not None:
				self.expression(node.value, scope, seen)
				value = self.value(node.value, scope)
				if isinstance(node, ast.YieldFrom):
					value = self.consume(value, seen)
				if self.yield_values:
					self.yield_values[-1].append(value)
			return
		if isinstance(node, ast.NamedExpr):
			self.visit(node, scope, seen)
			return
		if isinstance(node, ast.Await):
			self.expression(node.value, scope, seen)
			self.call_values[node] = self.consume(self.value(node.value, scope), seen, awaiting=True)
			return
		if isinstance(node, ast.Starred) and isinstance(node.ctx, ast.Load):
			self.expression(node.value, scope, seen)
			value = self.value(node.value, scope)
			produced = self.consume(value, seen)
			self.call_values[node] = SourceValue(
				node, items=value.items if value.items is not None else {None: produced}
			)
			return
		if isinstance(node, ast.GeneratorExp):
			self.expression(node.generators[0].iter, scope, seen)
			deferred = DeferredExecution(
				None,
				[],
				{},
				expression=node,
				scope=scope,
				outer_iterable=self.value(node.generators[0].iter, scope),
			)
			self.call_values[node] = SourceValue(node, deferred=(deferred,))
			return
		if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
			local = SourceScope(scope, kind="function")
			for generator in node.generators:
				self.expression(generator.iter, local, seen)
				iterable = self.value(generator.iter, local)
				produced = self.consume(iterable, seen)
				values = list(iterable.items.values()) if iterable.items else []
				value = self.merge(values) if values else produced
				self.bind(generator.target, value, local, seen)
				for condition in generator.ifs:
					self.expression(condition, local, seen)
					self.protocol(self.value(condition, local), ("__bool__", "__len__"), [], seen)
			for element in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
				self.expression(element, local, seen)
			item = self.value(node.value if isinstance(node, ast.DictComp) else node.elt, local)
			self.call_values[node] = SourceValue(node, items={None: item}, origins=frozenset({node}))
			return
		if isinstance(node, ast.Lambda):
			for item in definition_expressions(node):
				self.expression(item, scope, seen)
			function = ast.copy_location(
				ast.FunctionDef(
					name="<lambda>",
					args=node.args,
					body=[ast.copy_location(ast.Return(value=node.body), node)],
					decorator_list=[],
				),
				node,
			)
			self.call_values[node] = SourceValue(node, helpers=(self.function(function, scope),))
			return
		for child in ast.iter_child_nodes(node):
			if isinstance(child, ast.expr):
				self.expression(child, scope, seen)
			elif isinstance(child, ast.keyword):
				self.expression(child.value, scope, seen)
		if isinstance(node, (ast.Set, ast.Dict)):
			for key in node.elts if isinstance(node, ast.Set) else node.keys:
				if key is not None:
					value = self.value(key, scope)
					self.protocol(value, ("__hash__", "__eq__"), [value], seen)
		if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
			base = self.value(node.value, scope)
			member = (base.items or {}).get(node.attr)
			if base.instance:
				self.protocol(base, ("__getattribute__",), [SourceValue(ast.Constant(node.attr))], seen)
				getters = (
					[helper for helper in member.helpers if helper.descriptor == "property"] if member else []
				)
				results = [self.call_helper(helper, [base], {}, seen) for helper in getters]
				if member and member.instance:
					results.append(
						self.protocol(member, ("__get__",), [base, SourceValue(ast.Constant(None))], seen)
					)
				if not member and "__getattr__" in (base.items or {}):
					results.append(
						self.protocol(base, ("__getattr__",), [SourceValue(ast.Constant(node.attr))], seen)
					)
				if results:
					self.call_values[node] = self.merge(results)
		if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
			base = self.value(node.value, scope)
			if base.instance:
				self.call_values[node] = self.protocol(
					base, ("__getitem__",), [self.value(node.slice, scope)], seen
				)
		if isinstance(node, ast.BinOp):
			left, right = self.value(node.left, scope), self.value(node.right, scope)
			stem = {
				ast.Add: "add",
				ast.Sub: "sub",
				ast.Mult: "mul",
				ast.MatMult: "matmul",
				ast.Div: "truediv",
				ast.FloorDiv: "floordiv",
				ast.Mod: "mod",
				ast.Pow: "pow",
				ast.LShift: "lshift",
				ast.RShift: "rshift",
				ast.BitOr: "or",
				ast.BitAnd: "and",
				ast.BitXor: "xor",
			}.get(type(node.op))
			if stem:
				self.protocol(left, (f"__{stem}__",), [right], seen)
				self.protocol(right, (f"__r{stem}__",), [left], seen)
		if isinstance(node, ast.UnaryOp):
			name = {ast.UAdd: "__pos__", ast.USub: "__neg__", ast.Invert: "__invert__"}.get(type(node.op))
			self.protocol(
				self.value(node.operand, scope), (name,) if name else ("__bool__", "__len__"), [], seen
			)
		if isinstance(node, ast.IfExp):
			self.protocol(self.value(node.test, scope), ("__bool__", "__len__"), [], seen)
		if isinstance(node, ast.BoolOp):
			for operand in node.values[:-1]:
				self.protocol(self.value(operand, scope), ("__bool__", "__len__"), [], seen)
		if isinstance(node, ast.FormattedValue):
			self.protocol(
				self.value(node.value, scope), ("__format__",), [SourceValue(ast.Constant(""))], seen
			)
		if isinstance(node, ast.Compare):
			for operator, comparator in zip(node.ops, node.comparators, strict=True):
				name = {
					ast.Eq: "__eq__",
					ast.NotEq: "__ne__",
					ast.Lt: "__lt__",
					ast.LtE: "__le__",
					ast.Gt: "__gt__",
					ast.GtE: "__ge__",
				}.get(type(operator))
				if name:
					self.protocol(
						self.value(node.left, scope), (name,), [self.value(comparator, scope)], seen
					)
					self.protocol(
						self.value(comparator, scope), (name,), [self.value(node.left, scope)], seen
					)
				if isinstance(operator, (ast.In, ast.NotIn)):
					self.protocol(
						self.value(comparator, scope),
						("__contains__", "__iter__"),
						[self.value(node.left, scope)],
						seen,
					)
		if not isinstance(node, ast.Call):
			return
		callee = self.value(node.func, scope)
		arguments = []
		for item in node.args:
			value = self.value(item, scope)
			arguments.extend(
				value.items.values() if isinstance(item, ast.Starred) and value.items else [value]
			)
		keywords = {}
		for item in node.keywords:
			value = self.value(item.value, scope)
			if item.arg is None and value.items:
				keywords.update(value.items)
			elif item.arg:
				keywords[item.arg] = value
		if isinstance(node.func, ast.Attribute):
			base = self.value(node.func.value, scope)
			result = self.container_call(node, base, arguments, keywords, seen)
			if result is not None:
				self.call_values[node] = result
				return
		namespace = bool(
			callee.modules & {f"builtins.{name}" for name in ("globals", "locals", "exec", "eval")}
		)
		if "builtins.vars" in callee.modules:
			namespace |= len(arguments) != 1 or not arguments[0].is_class or bool(keywords)
		changed = set()
		for reference in callee.modules:
			if ".__dict__." in reference:
				module, method = reference.split(".__dict__.", 1)
				if method not in {"get", "keys", "values", "items", "copy", "__getitem__"}:
					self.module_writes.setdefault(module, set()).add(None)
					if module in {"typing", "typing_extensions"}:
						changed.add(module)
		if callee.modules & {"builtins.setattr", "builtins.delattr"} and arguments:
			attribute = node.args[1] if len(node.args) > 1 else None
			if not isinstance(attribute, ast.Constant) or attribute.value == "TYPE_CHECKING":
				changed.update(arguments[0].modules & {"typing", "typing_extensions"})
		if callee.modules & {"builtins.setattr", "builtins.delattr"} and arguments:
			attribute = node.args[1] if len(node.args) > 1 else None
			attribute_name = (
				attribute.value
				if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)
				else None
			)
			self.mark_members(arguments[0], attribute_name)
			for module in arguments[0].modules:
				self.module_writes.setdefault(module, set()).add(attribute_name)
		if callee.uncertain or (
			not callee.helpers
			and not callee.is_class
			and not callee.modules & {f"builtins.{name}" for name in self.BUILTINS}
		):
			changed.update(
				frozenset().union(
					*(self.reachable_modules(value) for value in [*arguments, *keywords.values()])
				)
				& {"typing", "typing_extensions"}
			)
		references = self.reachable(callee)
		consumers = {
			f"builtins.{name}"
			for name in ("list", "tuple", "set", "frozenset", "dict", "next", "sum", "any", "all", "sorted")
		} | {"asyncio.run"}
		readers = {
			f"builtins.{name}"
			for name in ("iter", "print", "id", "type", "repr", "str", "bool", "len", "hash", "format")
		}
		if callee.uncertain or (not callee.helpers and not (callee.modules & (consumers | readers))):
			references |= frozenset().union(
				*(self.reachable(item) for item in [*arguments, *keywords.values()] if not item.deferred)
			)
		if callee.uncertain:
			references |= frozenset().union(*(self.reachable(value) for value in scope.root.values.values()))
		# Reading vars(cls) does not itself mutate the class; calls on its result may.
		if "builtins.vars" in callee.modules and not namespace and not callee.uncertain:
			references = frozenset()
		uncertain_callback = bool(
			callee.modules
			& {
				"builtins.bool",
				"builtins.str",
				"builtins.repr",
				"builtins.len",
				"builtins.hash",
				"builtins.format",
			}
		) and any(
			not value.scalar and value.items is None and not value.instance and not value.is_class
			for value in arguments
		)
		known_iterator = (
			isinstance(node.func, ast.Attribute)
			and bool(self.value(node.func.value, scope).deferred)
			and node.func.attr in {"__next__", "send", "throw", "close", "aclose", *self.ASYNC_ADVANCES}
		)
		self.emit(
			SourceEffect(
				self.value(node, scope).expression,
				namespace=namespace,
				typing_modules=frozenset(changed),
				origins=references,
				opaque=not namespace
				and not known_iterator
				and (
					uncertain_callback
					or callee.uncertain
					or (
						not callee.helpers
						and not callee.is_class
						and not callee.modules & (consumers | readers)
						and not (callee.modules & {"builtins.vars", "builtins.setattr", "builtins.delattr"})
					)
				),
			)
		)
		results = []
		for helper in callee.helpers:
			if helper.deferred:
				results.append(
					SourceValue(
						node, deferred=(DeferredExecution(helper, [*callee.bound, *arguments], keywords),)
					)
				)
			else:
				results.append(self.invoke(helper, [*callee.bound, *arguments], keywords, seen))
		produced = []
		if callee.uncertain or (not callee.helpers and not callee.modules & (consumers | readers)):
			for argument in [*arguments, *keywords.values()]:
				produced.append(self.consume(argument, seen))
		elif callee.modules & consumers:
			for argument in arguments:
				produced.append(self.consume(argument, seen))
			if "asyncio.run" in callee.modules and "main" in keywords:
				produced.append(self.consume(keywords["main"], seen, awaiting=True))
		if callee.modules & {
			"builtins.list",
			"builtins.tuple",
			"builtins.set",
			"builtins.dict",
			"builtins.sorted",
		}:
			items = dict(arguments[0].items) if arguments and arguments[0].items is not None else {}
			keys = None
			if "builtins.dict" in callee.modules:
				items.update(keywords)
				keys = dict(arguments[0].key_values or {}) if arguments else {}
				keys.update({name: SourceValue(ast.Constant(name), scalar=True) for name in keywords})
			elif arguments and arguments[0].key_values is not None:
				items = dict(enumerate(arguments[0].key_values.values()))
			elif produced and not items:
				items[None] = self.merge(produced)
			results.append(SourceValue(node, items=items, key_values=keys, origins=frozenset({node})))
		elif "builtins.next" in callee.modules and produced:
			results.append(self.merge(produced))
		if callee.instance:
			results.append(self.protocol(callee, ("__call__",), arguments, seen))
		if callee.is_class:
			self.protocol(callee, ("__new__",), arguments, seen)
			instance = SourceValue(
				node, items=dict(callee.items or {}), origins=frozenset({node}), instance=True
			)
			initializer = (callee.items or {}).get("__init__")
			for helper in initializer.helpers if initializer else ():
				self.invoke(helper, [instance, *arguments], keywords, seen)
			results.append(instance)
		if "asyncio.run" in callee.modules and produced:
			results.append(self.merge(produced))
		for builtin, methods in {
			"bool": ("__bool__", "__len__"),
			"len": ("__len__",),
			"str": ("__str__",),
			"repr": ("__repr__",),
		}.items():
			if f"builtins.{builtin}" in callee.modules and arguments:
				results.append(self.protocol(arguments[0], methods, [], seen))
		if "builtins.iter" in callee.modules and arguments:
			value = arguments[0]
			results.append(
				self.protocol(value, ("__iter__",), [], seen)
				if value.instance and "__iter__" in (value.items or {})
				else value
			)
		if "builtins.vars" in callee.modules and arguments:
			results.append(SourceValue(node, origins=self.reachable(arguments[0])))
		if callee.modules & {"builtins.any", "builtins.all"}:
			for item in produced:
				self.truth(item, seen)
		if "builtins.print" in callee.modules:
			for argument in arguments:
				self.protocol(argument, ("__str__", "__repr__"), [], seen)
		if "builtins.hash" in callee.modules and arguments:
			self.protocol(arguments[0], ("__hash__",), [], seen)
		if "builtins.format" in callee.modules and arguments:
			self.protocol(
				arguments[0], ("__format__",), arguments[1:] or [SourceValue(ast.Constant(""))], seen
			)
		if callee.modules & {"builtins.set", "builtins.frozenset", "builtins.dict"}:
			for item in produced:
				key = (item.items or {}).get(0, item) if "builtins.dict" in callee.modules else item
				self.protocol(key, ("__hash__", "__eq__"), [key], seen)
		if "builtins.sorted" in callee.modules:
			for item in produced:
				key = keywords.get("key")
				key_values = [self.callback(key, [item], node, seen)] if key else [item]
				for key_value in key_values:
					self.protocol(key_value, ("__lt__", "__gt__"), [key_value], seen)

		if isinstance(node.func, ast.Attribute):
			iterator = self.value(node.func.value, scope)
			if node.func.attr in {"__next__", "send", "throw"}:
				results.append(self.consume(iterator, seen))
			elif node.func.attr in self.ASYNC_ADVANCES and iterator.deferred:
				results.append(
					SourceValue(node, deferred=(DeferredExecution(None, [], {}, advance=iterator),))
				)
		if results:
			self.call_values[node] = self.merge(results)

	def consume(self, value, seen, *, awaiting=False):
		value = self.project_view(value)
		produced = []
		for deferred in value.deferred:
			if id(deferred) in seen:
				continue
			if deferred.advance is not None:
				produced.append(self.consume(deferred.advance, seen | {id(deferred)}))
			elif deferred.helper:
				produced.append(
					self.invoke(
						deferred.helper,
						deferred.arguments,
						deferred.keywords,
						seen | {id(deferred)},
						yielding=True,
					)
				)
			else:
				local = SourceScope(deferred.scope, kind="function")
				for index, generator in enumerate(deferred.expression.generators):
					if index:
						self.expression(generator.iter, local, seen)
					iterable = self.value(generator.iter, local) if index else deferred.outer_iterable
					item = self.consume(iterable, seen | {id(deferred)})
					values = list(iterable.items.values()) if iterable.items else []
					self.bind(generator.target, self.merge(values) if values else item, local, seen)
					for condition in generator.ifs:
						self.expression(condition, local, seen)
				self.expression(deferred.expression.elt, local, seen)
				produced.append(self.value(deferred.expression.elt, local))
		if not value.deferred and value.instance and id(value) not in seen:
			for name in ("__await__",) if awaiting else ("__iter__", "__next__"):
				method = (value.items or {}).get(name)
				for helper in method.helpers if method else ():
					item = self.invoke(helper, [value], {}, seen | {id(value)}, yielding=not awaiting)
					produced.append(item)
					if name == "__iter__" and item is not value:
						produced.append(self.consume(item, seen | {id(value)}))
		if not value.deferred and value.items is not None and not value.instance and not value.is_class:
			produced.extend(
				value.key_values.values() if value.key_values is not None else value.items.values()
			)
		return self.merge(produced) if produced else SourceValue(ast.Constant(None))

	@staticmethod
	def always_returns(body):
		for statement in body:
			if isinstance(statement, (ast.Return, ast.Raise)):
				return True
			if (
				isinstance(statement, ast.If)
				and EffectAnalyzer.always_returns(statement.body)
				and EffectAnalyzer.always_returns(statement.orelse)
			):
				return True
		return False

	def invoke(self, helper, arguments, keywords, seen, *, yielding=False):
		function, closure = helper.node, helper.closure
		if id(function) in seen:
			# A recursion cutoff is unknown, never evidence that effects disappear.
			visible = [*arguments, *keywords.values(), *closure.root.values.values()]
			self.emit(
				SourceEffect(function, origins=frozenset().union(*(self.reachable(item) for item in visible)))
			)
			return self.merge(visible) if visible else SourceValue(ast.Constant(None))
		local = SourceScope(closure, kind="function", body=function.body)
		params = [*function.args.posonlyargs, *function.args.args]
		for parameter in [*params, *function.args.kwonlyargs]:
			local.values[parameter.arg] = SourceValue(ast.Constant(None))
		local.values.update(helper.defaults)
		for parameter, argument in zip(params, arguments, strict=False):
			local.values[parameter.arg] = argument
		keyword_parameters = {parameter.arg for parameter in [*function.args.args, *function.args.kwonlyargs]}
		local.values.update({name: value for name, value in keywords.items() if name in keyword_parameters})
		if None in keywords:
			for name in keyword_parameters:
				local.values[name] = self.merge([local.values[name], keywords[None]])
		if function.args.vararg:
			items = dict(enumerate(arguments[len(params) :]))
			local.values[function.args.vararg.arg] = SourceValue(
				ast.Tuple(elts=[item.expression for item in items.values()], ctx=ast.Load()), items=items
			)
		if function.args.kwarg:
			items = {name: value for name, value in keywords.items() if name not in keyword_parameters}
			local.values[function.args.kwarg.arg] = SourceValue(
				ast.Dict(
					keys=[ast.Constant(name) for name in items],
					values=[item.expression for item in items.values()],
				),
				items=items,
			)
		self.return_values.append([])
		self.yield_values.append([])
		for statement in function.body:
			if self.visit(statement, local, seen | {id(function)}):
				break
		results = self.return_values.pop()
		yielded = self.yield_values.pop()
		if yielding and yielded:
			return self.merge(yielded)
		if not self.always_returns(function.body) or not results:
			results.append(SourceValue(ast.Constant(None)))
		return self.merge(results)

	def bind(self, target, value, scope, seen=frozenset(), *, anchor=True):
		if isinstance(target, ast.Name):
			if scope.set(target.id, value, anchor=anchor):
				self.emit(SourceEffect(target, bindings=frozenset({target.id})))
		elif isinstance(target, (ast.Tuple, ast.List)):
			entries = list((value.items or {}).values())
			for index, item in enumerate(target.elts):
				if isinstance(item, ast.Starred):
					stop = len(entries) - (len(target.elts) - index - 1)
					selected = entries[index:stop]
					self.bind(
						item.value,
						SourceValue(item, items=dict(enumerate(selected)), origins=frozenset({item})),
						scope,
						seen,
						anchor=anchor,
					)
				else:
					position = (
						index
						if not any(isinstance(entry, ast.Starred) for entry in target.elts[:index])
						else index - len(target.elts)
					)
					selected = (
						entries[position]
						if -len(entries) <= position < len(entries)
						else SourceValue(ast.Constant(None))
					)
					self.bind(item, selected, scope, seen, anchor=anchor)
		else:
			self.expression(target, scope, seen)
			base = self.value(target.value, scope)
			if isinstance(target, ast.Subscript):
				try:
					attribute = ast.literal_eval(target.slice)
				except ValueError, TypeError:
					attribute = None
				self.mark_members(base, attribute if isinstance(attribute, str) else None)
				for module in base.modules:
					if module.endswith(".__dict__"):
						name = module.removesuffix(".__dict__")
						self.module_writes.setdefault(name, set()).add(attribute)
						if name in {"typing", "typing_extensions"} and attribute in {None, "TYPE_CHECKING"}:
							self.emit(SourceEffect(target, typing_modules=frozenset({name})))
			if isinstance(target, ast.Subscript) and base.items is not None:
				try:
					index = ast.literal_eval(target.slice)
				except ValueError, TypeError:
					index = None
				if base.key_values is not None:
					base.key_values[index] = self.value(target.slice, scope)
				previous = base.items.get(index)
				base.items[index] = (
					self.merge([*base.items.values(), value])
					if index is None
					else self.merge([previous, value])
					if self.branch_depth and previous is not None
					else value
				)
			if isinstance(target, ast.Attribute):
				for module in base.modules:
					self.module_writes.setdefault(module, set()).add(target.attr)
				if base.instance:
					self.protocol(
						base,
						("__delattr__",) if isinstance(target.ctx, ast.Del) else ("__setattr__",),
						[SourceValue(ast.Constant(target.attr)), value],
						seen,
					)
				self.mark_members(base, target.attr)
				if base.items is not None:
					base.items[target.attr] = value
			modules = (
				self.value(target.value, scope).modules
				if isinstance(target, ast.Attribute) and target.attr == "TYPE_CHECKING"
				else frozenset()
			)
			self.emit(
				SourceEffect(
					self.value(target, scope).expression,
					typing_modules=modules & {"typing", "typing_extensions"},
					origins=self.reachable(self.value(target.value, scope)),
					mutation=True,
				)
			)

	def branches(self, blocks, scope, seen, *, include_initial=True):
		scopes = []
		owner = scope
		while owner:
			scopes.append(owner)
			owner = owner.parent
		before = [item.values.copy() for item in scopes]
		outcomes = [before] if include_initial else []
		self.branch_depth += 1
		for block in blocks:
			for item, values in zip(scopes, before, strict=True):
				item.values = values.copy()
			for statement in block:
				if self.visit(statement, scope, seen):
					break
			outcomes.append([item.values.copy() for item in scopes])
		self.branch_depth -= 1
		for index, owner in enumerate(scopes):
			owner.values = {}
			for name in set().union(*(outcome[index] for outcome in outcomes)):
				values = [outcome[index].get(name, SourceValue(ast.Constant(None))) for outcome in outcomes]
				owner.values[name] = self.merge(values)

	@staticmethod
	def merge(values, seen=frozenset()):
		if all(value is values[0] for value in values):
			return values[0]
		group = tuple(sorted(id(value) for value in values))
		if group in seen:
			return SourceValue(
				ast.Constant(None),
				origins=frozenset().union(*(EffectAnalyzer.reachable(value) for value in values)),
				uncertain=True,
				instance=any(value.instance for value in values),
			)
		seen = seen | {group}
		keys = set().union(*(value.items or {} for value in values))
		items = (
			{
				key: EffectAnalyzer.merge(
					[value.items[key] for value in values if value.items is not None and key in value.items],
					seen,
				)
				for key in keys
			}
			if keys
			else None
		)
		return SourceValue(
			ast.Tuple(elts=[value.expression for value in values], ctx=ast.Load()),
			items=items,
			key_values={
				key: EffectAnalyzer.merge(
					[
						value.key_values[key]
						for value in values
						if value.key_values is not None and key in value.key_values
					],
					seen,
				)
				for key in set().union(*(value.key_values or {} for value in values))
			}
			if any(value.key_values is not None for value in values)
			else None,
			instance=any(value.instance for value in values),
			scalar=all(value.scalar for value in values),
			bound=tuple(
				EffectAnalyzer.merge(list(entries), seen)
				for entries in zip(*(value.bound for value in values), strict=True)
			)
			if all(len(value.bound) == len(values[0].bound) for value in values)
			else (),
			modules=frozenset().union(*(value.modules for value in values)),
			helpers=tuple({id(helper): helper for value in values for helper in value.helpers}.values()),
			origins=frozenset().union(*(value.origins for value in values)),
			deferred=tuple({id(item): item for value in values for item in value.deferred}.values()),
			false_constant=all(value.false_constant for value in values),
			uncertain=any(value.uncertain for value in values)
			or any(len(value.bound) != len(values[0].bound) for value in values)
			or any(
				value.modules != values[0].modules or value.origins != values[0].origins for value in values
			),
			is_class=all(value.is_class for value in values),
		)

	@staticmethod
	def expression_nodes(node):
		if isinstance(node, (*DEFINITION_NODES, ast.Lambda)):
			for expression in definition_expressions(node):
				yield from EffectAnalyzer.expression_nodes(expression)
			return
		yield node
		for child in ast.iter_child_nodes(node):
			yield from EffectAnalyzer.expression_nodes(child)

	def container_call(self, node, base, arguments, keywords, seen):
		name = node.func.attr
		if base.items is None or base.instance or base.is_class:
			return None
		if name in {"append", "extend", "insert", "update", "setdefault", "__setitem__"}:
			values = list(arguments)
			if name in {"extend", "update"} and arguments:
				produced = self.consume(arguments[0], seen)
				values = list((arguments[0].items or {}).values()) or [produced]
			if name == "update":
				if base.key_values is not None:
					base.key_values.update(arguments[0].key_values or {} if arguments else {})
					base.key_values.update(
						{key: SourceValue(ast.Constant(key), scalar=True) for key in keywords}
					)
				base.items.update(arguments[0].items or {} if arguments else {})
				base.items.update(keywords)
			else:
				value = values[-1] if values else SourceValue(ast.Constant(None))
				if name == "setdefault" and len(arguments) > 1:
					try:
						key = ast.literal_eval(arguments[0].expression)
					except TypeError, ValueError:
						key = None
					base.items[key] = self.merge([base.items[key], value]) if key in base.items else value
				elif name == "extend":
					for value in values:
						base.items[len(base.items)] = value
				else:
					key = len(base.items) if name == "append" else None
					base.items[key] = self.merge([base.items[key], value]) if key in base.items else value
			self.emit(SourceEffect(node, origins=base.origins, mutation=True, shallow=True))
			return value if name == "setdefault" else SourceValue(ast.Constant(None))
		if name in {"get", "pop", "__getitem__"}:
			try:
				key = (
					ast.literal_eval(arguments[0].expression)
					if arguments
					else max((key for key in base.items if isinstance(key, int)), default=None)
				)
			except ValueError, TypeError:
				key = None
			values = [base.items[key]] if key in base.items and key is not None else list(base.items.values())
			if None in base.items:
				values.append(base.items[None])
			if name == "get" and len(arguments) > 1:
				values.append(arguments[1])
			if name == "pop":
				self.emit(SourceEffect(node, origins=base.origins, mutation=True, shallow=True))
			return self.merge(values) if values else SourceValue(ast.Constant(None))
		if name == "copy":
			return SourceValue(
				node,
				items=dict(base.items),
				key_values=dict(base.key_values) if base.key_values is not None else None,
				origins=frozenset({node}),
			)
		if name in {"keys", "values", "items"}:
			return self.project_view(SourceValue(node, view=(name, base), origins=frozenset({node})))
		return None

	@staticmethod
	def project_view(value):
		if value.view is None:
			return value
		kind, base = value.view
		entries = []
		for key, item in (base.items or {}).items():
			key_value = (base.key_values or {}).get(key, SourceValue(ast.Constant(key), scalar=True))
			entries.append(
				item
				if kind == "values"
				else key_value
				if kind == "keys"
				else SourceValue(value.expression, items={0: key_value, 1: item}, origins=value.origins)
			)
		return replace(value, items=dict(enumerate(entries)))

	def callback(self, callee, arguments, node, seen):
		# Indirect callbacks use exactly the same dispatch and uncertainty rules as calls.
		function = ast.copy_location(ast.Name(id="_callback", ctx=ast.Load()), node)
		self.call_values[function] = callee
		parameters = []
		for index, value in enumerate(arguments):
			parameter = ast.copy_location(ast.Name(id=f"_argument_{index}", ctx=ast.Load()), node)
			self.call_values[parameter] = value
			parameters.append(parameter)
		call = ast.copy_location(ast.Call(func=function, args=parameters, keywords=[]), node)
		self.expression(call, self.scope, seen)
		return self.call_values.get(call, SourceValue(call, uncertain=True))

	def call_helper(self, helper, arguments, keywords, seen):
		if helper.deferred:
			return SourceValue(helper.node, deferred=(DeferredExecution(helper, arguments, keywords),))
		return self.invoke(helper, arguments, keywords, seen)

	def truth(self, value, seen):
		name = "__bool__" if "__bool__" in (value.items or {}) else "__len__"
		return self.protocol(value, (name,), [], seen)

	def protocol(self, value, names, arguments, seen):
		results = []
		for name in names:
			method = (value.items or {}).get(name)
			for helper in method.helpers if method else ():
				results.append(self.call_helper(helper, [value, *arguments], {}, seen))
		return self.merge(results) if results else SourceValue(ast.Constant(None))

	def mark_members(self, value, name):
		for origin in value.origins:
			if isinstance(origin, ast.ClassDef):
				self.member_writes.setdefault(origin, set()).add(name)
			elif isinstance(origin, FUNCTION_NODES) and (
				name is None
				or name in {"__code__", "__defaults__", "__kwdefaults__", "__doc__", "__wrapped__"}
			):
				self.invalid_definitions.add(origin)

	def function(self, node, scope):
		closure = scope
		while closure.kind == "class":
			closure = closure.parent
		params = [*node.args.posonlyargs, *node.args.args]
		defaults = {
			parameter.arg: self.value(default, scope)
			for parameter, default in zip(
				params[-len(node.args.defaults) :] if node.args.defaults else [],
				node.args.defaults,
				strict=True,
			)
		}
		defaults.update(
			{
				parameter.arg: self.value(default, scope)
				for parameter, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
				if default is not None
			}
		)
		deferred = isinstance(node, ast.AsyncFunctionDef) or any(
			isinstance(child, (ast.Yield, ast.YieldFrom))
			for statement in scope_statements(node.body)
			for child in self.expression_nodes(statement)
		)
		descriptor = None
		if scope.kind == "class":
			decorators = frozenset().union(
				*(
					self.value(item, scope).modules
					for item in node.decorator_list
					if not self.value(item, scope).uncertain
				)
			)
			descriptor = (
				"static"
				if "builtins.staticmethod" in decorators
				else "class"
				if "builtins.classmethod" in decorators
				else "property"
				if "builtins.property" in decorators
				else "instance"
			)
		return SourceFunction(node, closure, defaults, deferred, descriptor)

	def visit(self, node, scope, seen):
		if isinstance(node, ast.AnnAssign) and node.value is None:
			return False
		if isinstance(node, DEFINITION_NODES):
			for expression in definition_expressions(node):
				self.expression(expression, scope, seen)
			replaced = False
			argument = SourceValue(
				ast.Constant(None), is_class=isinstance(node, ast.ClassDef), origins=frozenset({node})
			)
			for decorator in node.decorator_list:
				callee = self.value(decorator, scope)
				for helper in callee.helpers:
					function = helper.node
					identity = False
					start = len(self.effects)
					if not helper.deferred:
						result = self.invoke(helper, [argument], {}, seen)
						identity = (
							self.always_returns(function.body)
							and not result.uncertain
							and result.origins == argument.origins
						)
					replaced |= (
						not identity
						or helper.deferred
						or any(node in effect.origins for effect in self.effects[start:])
					)
			if isinstance(node, ast.ClassDef):
				local = SourceScope(scope, kind="class", body=node.body)
				active = []
				for statement in node.body:
					statements = (
						statement.orelse
						if isinstance(statement, ast.If) and self.value(statement.test, local).false_constant
						else [statement]
					)
					for statement in statements:
						active.append(statement)
						self.visit(statement, local, seen)
				self.runtime_bodies[node] = active
			helpers = (
				(self.function(node, scope),) if isinstance(node, FUNCTION_NODES) and not replaced else ()
			)
			inherited = {}
			if isinstance(node, ast.ClassDef):
				for base in reversed(node.bases):
					inherited.update(self.value(base, scope).items or {})
				inherited.update(local.values)
			value = SourceValue(
				ast.Constant(None),
				helpers=helpers,
				is_class=isinstance(node, ast.ClassDef) and not replaced,
				origins=frozenset({node}),
				items=inherited if isinstance(node, ast.ClassDef) else None,
			)
			if isinstance(node, ast.ClassDef):
				for base in node.bases:
					base_value = self.value(base, scope)
					method = (base_value.items or {}).get("__init_subclass__")
					for helper in method.helpers if method else ():
						self.call_helper(helper, [value], {}, seen)
					self.protocol(
						base_value,
						("__mro_entries__",),
						[SourceValue(ast.Tuple(elts=[], ctx=ast.Load()))],
						seen,
					)
				for keyword in node.keywords:
					if keyword.arg == "metaclass":
						self.protocol(
							self.value(keyword.value, scope),
							("__new__", "__init__"),
							[
								SourceValue(ast.Constant(node.name)),
								SourceValue(ast.Tuple(elts=[], ctx=ast.Load())),
								SourceValue(
									ast.Constant(None), items=local.values, origins=frozenset({node})
								),
							],
							seen,
						)
				for name, descriptor in local.values.items():
					if descriptor.instance:
						self.protocol(
							descriptor, ("__set_name__",), [value, SourceValue(ast.Constant(name))], seen
						)
			self.bind(ast.copy_location(ast.Name(id=node.name, ctx=ast.Store()), node), value, scope)
			if replaced:
				self.invalid_definitions.add(node)
				self.emit(SourceEffect(node, replaced_definition=node.name))
		elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
			self.expression(node.value, scope, seen)
			value = self.value(node.value, scope)
			for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
				self.bind(target, value, scope, seen)
		elif isinstance(node, ast.AugAssign):
			self.expression(node.value, scope, seen)
			previous = self.value(node.target, scope)
			addition = self.value(node.value, scope)
			self.emit(SourceEffect(node, mutation=True, origins=self.reachable(previous)))
			items = None
			if previous.items is not None and addition.items is not None:
				items = (
					dict(enumerate([*previous.items.values(), *addition.items.values()]))
					if isinstance(node.op, ast.Add)
					else {**previous.items, **addition.items}
				)
			value = replace(
				previous, origins=previous.origins | addition.origins, items=items, uncertain=True
			)
			self.bind(node.target, value, scope, seen)
		elif isinstance(node, ast.Delete):
			for target in node.targets:
				self.bind(target, SourceValue(ast.Constant(None)), scope, seen)
		elif isinstance(node, (ast.Import, ast.ImportFrom)):
			if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
				# A wildcard may replace an earlier class with a module or another object.
				for name, value in scope.values.items():
					scope.values[name] = replace(value, is_class=False, false_constant=False, uncertain=True)
			for alias in node.names:
				name = alias.asname or (
					alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
				)
				module = (
					alias.name
					if isinstance(node, ast.Import)
					else f"{node.module}.{alias.name}"
					if not node.level
					else ""
				)
				self.bind(
					ast.copy_location(ast.Name(id=name, ctx=ast.Store()), node),
					SourceValue(
						ast.Constant(None),
						modules=frozenset({module}) if module else frozenset(),
						false_constant=module in {"typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"}
						and node.module not in self.changed_typing,
					),
					scope,
				)
		elif isinstance(node, (ast.With, ast.AsyncWith)):
			contexts = []
			for item in node.items:
				self.expression(item.context_expr, scope, seen)
				context = self.value(item.context_expr, scope)
				entry = (
					self.consume(context, seen)
					if context.deferred
					else self.protocol(context, ("__enter__", "__aenter__"), [], seen)
				)
				contexts.append(context)
				if item.optional_vars:
					self.bind(item.optional_vars, entry, scope, seen)
			for statement in node.body:
				if self.visit(statement, scope, seen):
					break
			for context in reversed(contexts):
				self.protocol(context, ("__exit__", "__aexit__"), [SourceValue(ast.Constant(None))] * 3, seen)
		elif isinstance(node, (ast.While, ast.Assert)):
			self.expression(node.test, scope, seen)
			self.protocol(self.value(node.test, scope), ("__bool__", "__len__"), [], seen)
			if isinstance(node, ast.While):
				self.branches([node.body, node.orelse], scope, seen)
		elif isinstance(node, ast.If):
			self.expression(node.test, scope, seen)
			self.protocol(self.value(node.test, scope), ("__bool__", "__len__"), [], seen)
			if self.value(node.test, scope).false_constant:
				for statement in node.orelse:
					if self.visit(statement, scope, seen):
						return True
			else:
				self.branches([node.body, node.orelse], scope, seen, include_initial=False)
		elif isinstance(node, (ast.For, ast.AsyncFor)):
			self.expression(node.iter, scope, seen)
			iterable = self.value(node.iter, scope)
			produced = self.consume(iterable, seen)
			values = list(iterable.items.values()) if iterable.items else []
			self.bind(
				node.target,
				self.merge(values) if values else produced,
				scope,
				seen,
				anchor=False,
			)
			self.branches([node.body], scope, seen)
			for statement in node.orelse:
				self.visit(statement, scope, seen)
		elif isinstance(node, ast.Return):
			if node.value:
				self.expression(node.value, scope, seen)
			if self.return_values:
				self.return_values[-1].append(
					self.value(node.value, scope) if node.value else SourceValue(ast.Constant(None))
				)
		elif isinstance(node, (ast.Global, ast.Nonlocal)):
			pass
		else:
			blocks = []
			for _, field in ast.iter_fields(node):
				if isinstance(field, ast.expr):
					self.expression(field, scope, seen)
				elif isinstance(field, list):
					statements = [child for child in field if isinstance(child, ast.stmt)]
					if statements:
						blocks.append(statements)
					for child in field:
						if isinstance(child, (ast.ExceptHandler, ast.match_case)):
							blocks.append(child.body)
						elif isinstance(child, ast.withitem):
							self.expression(child.context_expr, scope, seen)
			if blocks:
				self.branches(blocks, scope, seen)
		return isinstance(node, (ast.Return, ast.Raise))
