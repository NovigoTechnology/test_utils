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
	declared: dict | None = None
	class_order: tuple = ()


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
	callback: SourceValue | None = None
	sentinel: SourceValue | None = None
	sequence: SourceValue | None = None


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
	argument_origins: frozenset = frozenset()


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
		self.scope.values["NotImplemented"] = SourceValue(
			ast.Name(id="NotImplemented", ctx=ast.Load()),
			modules=frozenset({"builtins.NotImplemented"}),
			scalar=True,
		)
		self.effects = []
		self.call_values = {}
		self.invalid_definitions = set()
		self.default_owners = {}
		self.runtime_bodies = {}
		self.changed_typing = set()
		self.module_writes = {}
		self.return_values = []
		self.yield_values = []
		self.member_writes = {}
		self.branch_depth = 0

	def emit(self, effect):
		self.effects.append(effect)
		if effect.mutation or effect.opaque or effect.namespace:
			for origin in effect.origins:
				self.invalid_definitions.update(self.default_owners.get(origin, ()))
		for origin in effect.origins:
			if (
				isinstance(origin, tuple)
				and len(origin) == 2
				and isinstance(origin[0], FUNCTION_NODES)
				and origin[1] in {"__kwdefaults__", "__defaults__"}
			):
				self.invalid_definitions.add(origin[0])
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
	def exposed_origins(value, seen=frozenset()):
		"""Keep passed object identities without treating their namespaces as passed objects."""
		if id(value) in seen:
			return frozenset()
		references = {
			origin[1]
			for origin in value.origins
			if isinstance(origin, tuple)
			and len(origin) == 2
			and origin[0] == "import"
			and isinstance(origin[1], str)
		}
		origins = frozenset(
			origin
			for origin in value.origins
			if not (
				isinstance(origin, tuple)
				and len(origin) == 2
				and origin[0] == "import"
				and isinstance(origin[1], str)
			)
			or origin[1] in value.modules
			or not any(reference.startswith(origin[1] + ".") for reference in references | value.modules)
		)
		return origins | frozenset().union(
			*(
				EffectAnalyzer.exposed_origins(item, seen | {id(value)})
				for item in (value.items or {}).values()
			)
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

	@staticmethod
	def default_alias(value, container):
		owners = frozenset(
			origin
			for origin in container.origins
			if isinstance(origin, tuple) and len(origin) == 2 and isinstance(origin[0], FUNCTION_NODES)
		)
		return replace(value, origins=value.origins | owners) if owners and not value.scalar else value

	def sequence_kind(self, value):
		if value.items is None or value.uncertain or value.instance or value.is_class:
			return None
		kinds = set()
		for origin in value.origins:
			if isinstance(origin, (ast.List, ast.Tuple)):
				kinds.add(type(origin))
			elif (
				isinstance(origin, ast.BinOp)
				and isinstance(origin.op, ast.Add)
				and origin in self.call_values
			):
				kinds.add(type(self.call_values[origin].expression))
			else:
				return None
		return next(iter(kinds)) if len(kinds) == 1 and kinds <= {ast.List, ast.Tuple} else None

	def value(self, node, scope):
		if node in self.call_values:
			return self.call_values[node]
		if isinstance(node, ast.Name):
			return self.project_view(scope.get(node.id))
		if isinstance(node, ast.Constant):
			return SourceValue(node, scalar=True)
		if isinstance(node, ast.Attribute):
			base = self.value(node.value, scope)
			if node.attr in {"__kwdefaults__", "__defaults__"}:
				owners = frozenset(
					(origin, node.attr) for origin in base.origins if isinstance(origin, FUNCTION_NODES)
				)
				if owners:
					items = {}
					for helper in base.helpers:
						names = (
							{parameter.arg for parameter in helper.node.args.kwonlyargs}
							if node.attr == "__kwdefaults__"
							else {
								parameter.arg
								for parameter in [*helper.node.args.posonlyargs, *helper.node.args.args]
							}
						)
						items.update(
							{name: value for name, value in helper.defaults.items() if name in names}
						)
					return SourceValue(
						node,
						origins=owners,
						items=items,
						key_values={name: SourceValue(ast.Constant(name), scalar=True) for name in items}
						if node.attr == "__kwdefaults__"
						else None,
					)

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
				return replace(
					member,
					bound=bound,
					modules=member.modules | frozenset(f"{name}.{node.attr}" for name in base.modules),
					uncertain=member.uncertain or base.uncertain,
				)
			modules = frozenset(f"{name}.{node.attr}" for name in base.modules)
			return SourceValue(
				ast.copy_location(ast.Attribute(value=base.expression, attr=node.attr, ctx=node.ctx), node),
				modules=modules,
				uncertain=base.uncertain,
				origins=frozenset()
				if node.attr in {"__name__", "__qualname__", "__module__"}
				and (
					base.is_class
					or base.helpers
					or any(isinstance(origin, DEFINITION_NODES) for origin in base.origins)
				)
				else self.reachable(base) | frozenset(("import", name) for name in modules),
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
					selected = [self.default_alias(value, base) for value in list(base.items.values())[index]]
					return SourceValue(node, items=dict(enumerate(selected)), origins=frozenset({node}))
				index = ast.literal_eval(node.slice)
				if base.items is not None and index in base.items:
					return self.default_alias(
						(
							self.merge([base.items[index], base.items[None]])
							if None in base.items and index is not None
							else base.items[index]
						),
						base,
					)
			except ValueError, TypeError:
				pass
			possible = list((base.items or {}).values())
			result = self.merge(possible) if possible else SourceValue(node)
			return replace(result, origins=result.origins | self.reachable(base), uncertain=True)
		if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
			left, right = self.value(node.left, scope), self.value(node.right, scope)
			kind = self.sequence_kind(left)
			if kind is not None and kind is self.sequence_kind(right):
				items = dict(enumerate([*left.items.values(), *right.items.values()]))
				expression = kind(elts=[value.expression for value in items.values()], ctx=ast.Load())
				return SourceValue(
					ast.copy_location(expression, node), items=items, origins=frozenset({node})
				)
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
				outer_iterable=self.acquire(
					self.value(node.generators[0].iter, scope),
					seen,
					asynchronous=bool(node.generators[0].is_async),
				),
			)
			self.call_values[node] = SourceValue(node, deferred=(deferred,))
			return
		if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
			local = SourceScope(scope, kind="function")
			for generator in node.generators:
				self.expression(generator.iter, local, seen)
				iterable = self.value(generator.iter, local)
				produced = (
					self.consume_async(iterable, seen) if generator.is_async else self.consume(iterable, seen)
				)
				values = list(iterable.items.values()) if iterable.items else []
				value = self.merge(values) if values else produced
				self.bind(generator.target, value, local, seen)
				for condition in generator.ifs:
					self.expression(condition, local, seen)
					self.truth(self.value(condition, local), seen)
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
		if isinstance(node, ast.Compare):
			self.expression(node.left, scope, seen)
			left = self.value(node.left, scope)
			for index, (operator, comparator) in enumerate(zip(node.ops, node.comparators, strict=True)):
				self.expression(comparator, scope, seen)
				right = self.value(comparator, scope)
				result = self.comparison(left, right, operator, seen)
				self.call_values[node] = result
				if index < len(node.ops) - 1:
					truth = self.truth(result, seen)
					known = self.literal_truth(truth)
					if known is False or (known is None and self.literal_truth(result) is False):
						break
				left = right
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
			result = self.arithmetic(left, right, node.op, seen)
			if self.protocol_result(result) != "fallback":
				self.call_values[node] = result
			elif isinstance(node.op, ast.Add):
				result = self.value(node, scope)
				if result.origins == frozenset({node}) and not result.uncertain:
					self.call_values[node] = result
		if isinstance(node, ast.UnaryOp):
			name = {ast.UAdd: "__pos__", ast.USub: "__neg__", ast.Invert: "__invert__"}.get(type(node.op))
			if name:
				self.protocol(self.value(node.operand, scope), (name,), [], seen)
			else:
				operand = self.value(node.operand, scope)
				truth = self.truth(operand, seen)
				known = self.literal_truth(truth)
				if known is None:
					known = self.literal_truth(operand)
				self.call_values[node] = SourceValue(
					ast.Constant(not known) if known is not None else node,
					scalar=True,
					uncertain=known is None,
				)
		if isinstance(node, ast.IfExp):
			self.truth(self.value(node.test, scope), seen)
		if isinstance(node, ast.BoolOp):
			for operand in node.values[:-1]:
				self.truth(self.value(operand, scope), seen)
		if isinstance(node, ast.FormattedValue):
			value = self.value(node.value, scope)
			if node.conversion in {ord("r"), ord("a")}:
				value = self.stringify(value, seen, representation=True)
			elif node.conversion == ord("s"):
				value = self.stringify(value, seen)
			specification = (
				self.value(node.format_spec, scope)
				if node.format_spec
				else SourceValue(ast.Constant(""), scalar=True)
			)
			if "__format__" in (value.items or {}):
				self.protocol(value, ("__format__",), [specification], seen)
			elif value.instance:
				self.stringify(value, seen)
		if not isinstance(node, ast.Call):
			return
		callee = self.value(node.func, scope)
		if any(helper.node in self.invalid_definitions for helper in callee.helpers):
			callee = replace(callee, uncertain=True)
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
		effect = SourceEffect(
			self.value(node, scope).expression,
			namespace=namespace,
			typing_modules=frozenset(changed),
			origins=references,
			argument_origins=frozenset().union(
				*(self.exposed_origins(item) for item in [*callee.bound, *arguments, *keywords.values()])
			),
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
		self.emit(effect)
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
		if effect.opaque and produced:
			self.emit(
				SourceEffect(
					node,
					opaque=True,
					origins=frozenset().union(*(self.reachable(item) for item in produced)),
					argument_origins=frozenset().union(*(self.exposed_origins(item) for item in produced)),
				)
			)
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
				node,
				items=dict(callee.items or {}),
				origins=frozenset({node}),
				instance=True,
				uncertain=callee.uncertain,
				class_order=callee.class_order,
			)
			initializer = (callee.items or {}).get("__init__")
			for helper in initializer.helpers if initializer else ():
				self.invoke(helper, [instance, *arguments], keywords, seen)
			results.append(instance)
		if "asyncio.run" in callee.modules and produced:
			results.append(self.merge(produced))
		if "builtins.bool" in callee.modules and arguments:
			results.append(self.truth(arguments[0], seen))
		if "builtins.len" in callee.modules and arguments:
			results.append(self.protocol(arguments[0], ("__len__",), [], seen))
		if callee.modules & {"builtins.str", "builtins.repr"} and arguments:
			results.append(
				self.stringify(arguments[0], seen, representation="builtins.repr" in callee.modules)
			)
		if "builtins.iter" in callee.modules and arguments:
			results.append(
				SourceValue(
					node,
					deferred=(DeferredExecution(None, [], {}, callback=arguments[0], sentinel=arguments[1]),),
				)
				if len(arguments) == 2
				else self.acquire(arguments[0], seen)
			)
		if "builtins.vars" in callee.modules and arguments:
			results.append(SourceValue(node, origins=self.reachable(arguments[0])))
		if callee.modules & {"builtins.any", "builtins.all"}:
			for item in produced:
				self.truth(item, seen)
		if "builtins.print" in callee.modules:
			for argument in arguments:
				self.stringify(argument, seen)
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
				results.append(self.advance(iterator, seen))
			elif node.func.attr in self.ASYNC_ADVANCES and iterator.deferred:
				results.append(
					SourceValue(node, deferred=(DeferredExecution(None, [], {}, advance=iterator),))
				)
		if results:
			self.call_values[node] = self.merge(results)

	def acquire(self, value, seen, *, asynchronous=False):
		"""Obtain the iterator without advancing a generator or awaiting its next item."""
		value = self.project_view(value)
		name = "__aiter__" if asynchronous else "__iter__"
		if value.instance and name in (value.items or {}):
			return self.protocol(value, (name,), [], seen)
		if value.instance and not asynchronous and "__getitem__" in (value.items or {}):
			return SourceValue(value.expression, deferred=(DeferredExecution(None, [], {}, sequence=value),))
		if (
			not value.deferred
			and not value.scalar
			and value.items is None
			and not value.instance
			and not value.is_class
		):
			self.emit(
				SourceEffect(
					value.expression,
					opaque=True,
					origins=self.reachable(value),
					argument_origins=self.exposed_origins(value),
				)
			)
		return value

	def consume_async(self, value, seen):
		return self.advance_async(self.acquire(value, seen, asynchronous=True), seen)

	def advance_async(self, iterator, seen):
		if iterator.deferred:
			return self.advance(iterator, seen)
		step = self.protocol(iterator, ("__anext__",), [], seen)
		return self.advance(step, seen, awaiting=True) if step.deferred else step

	def consume(self, value, seen, *, awaiting=False):
		return self.advance(value if awaiting else self.acquire(value, seen), seen, awaiting=awaiting)

	def advance(self, value, seen, *, awaiting=False):
		"""Advance an already acquired iterator; do not repeat its __iter__ callback."""
		value = self.project_view(value)
		produced = []
		for deferred in value.deferred:
			if id(deferred) in seen:
				continue
			active = seen | {id(deferred)}
			if deferred.callback is not None:
				item = self.callback(deferred.callback, [], value.expression, active)
				if deferred.sentinel is not None:
					self.protocol(item, ("__eq__",), [deferred.sentinel], active)
					self.protocol(deferred.sentinel, ("__eq__",), [item], active)
				produced.append(item)
			elif deferred.sequence is not None:
				produced.append(
					self.protocol(
						deferred.sequence,
						("__getitem__",),
						[SourceValue(ast.Constant(0), scalar=True)],
						active,
					)
				)
			elif deferred.advance is not None:
				produced.append(self.advance(deferred.advance, active))
			elif deferred.helper:
				produced.append(
					self.invoke(
						deferred.helper, deferred.arguments, deferred.keywords, active, yielding=not awaiting
					)
				)
			else:
				local = SourceScope(deferred.scope, kind="function")
				for index, generator in enumerate(deferred.expression.generators):
					if index:
						self.expression(generator.iter, local, active)
						iterator = self.acquire(
							self.value(generator.iter, local), active, asynchronous=bool(generator.is_async)
						)
					else:
						iterator = deferred.outer_iterable
					item = (
						self.advance_async(iterator, active)
						if generator.is_async
						else self.advance(iterator, active)
					)
					values = list(iterator.items.values()) if iterator.items and not iterator.instance else []
					self.bind(generator.target, self.merge(values) if values else item, local, active)
					for condition in generator.ifs:
						self.expression(condition, local, active)
						self.truth(self.value(condition, local), active)
				self.expression(deferred.expression.elt, local, active)
				produced.append(self.value(deferred.expression.elt, local))
		if not value.deferred and value.instance and id(value) not in seen:
			name = "__await__" if awaiting else "__next__"
			method = (value.items or {}).get(name)
			for helper in method.helpers if method else ():
				item = self.invoke(helper, [value], {}, seen | {id(value)}, yielding=not awaiting)
				produced.append(item)
				if awaiting and (item.deferred or item.instance):
					produced.append(self.advance(item, seen | {id(value), id(helper.node)}, awaiting=True))
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
				SourceEffect(
					function,
					opaque=True,
					origins=frozenset().union(*(self.reachable(item) for item in visible)),
					argument_origins=frozenset().union(
						*(self.exposed_origins(item) for item in [*arguments, *keywords.values()])
					),
				)
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
				if base.instance:
					arguments = [self.value(target.slice, scope)] + (
						[] if isinstance(target.ctx, ast.Del) else [value]
					)
					self.protocol(
						base,
						("__delitem__",) if isinstance(target.ctx, ast.Del) else ("__setitem__",),
						arguments,
						seen,
					)
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
				if base.is_class and base.declared is not None:
					base.declared[target.attr] = value
			modules = (
				self.value(target.value, scope).modules
				if isinstance(target, ast.Attribute) and target.attr == "TYPE_CHECKING"
				else frozenset()
			)
			self.emit(
				SourceEffect(
					self.value(target, scope).expression,
					typing_modules=modules & {"typing", "typing_extensions"},
					origins=base.origins,
					mutation=True,
					shallow=True,
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
		if name in {"clear", "popitem"}:
			# These native operations change the container's structure, not its values.
			# A shallow copy can therefore be changed without mutating shared defaults.
			self.emit(SourceEffect(node, origins=base.origins, mutation=True, shallow=True))
			if name == "popitem":
				if not base.items:
					return SourceValue(node, uncertain=True)
				key = next(reversed(base.items))
				item = self.default_alias(base.items[key], base)
				key_value = (base.key_values or {}).get(key, SourceValue(ast.Constant(key), scalar=True))
				if not self.branch_depth:
					base.items.pop(key)
					if base.key_values is not None:
						base.key_values.pop(key, None)
				return SourceValue(node, items={0: key_value, 1: item}, origins=frozenset({node}))
			if not self.branch_depth:
				base.items.clear()
				if base.key_values is not None:
					base.key_values.clear()
			return SourceValue(ast.Constant(None), scalar=True)
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
				incoming = dict(arguments[0].items or {}) if arguments else {}
				incoming.update(keywords)
				for key, value in incoming.items():
					base.items[key] = (
						self.merge([base.items[key], value])
						if self.branch_depth and key in base.items
						else value
					)
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
			if base.key_values is not None and arguments and not arguments[0].scalar:
				# A mapping lookup may execute application-defined hash/equality code.
				# Unknown key behavior cannot prove that captured values stay unchanged.
				self.emit(
					SourceEffect(
						node,
						opaque=True,
						origins=self.reachable(base) | self.reachable(arguments[0]),
						argument_origins=self.exposed_origins(base) | self.exposed_origins(arguments[0]),
					)
				)
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
			return self.default_alias(self.merge(values), base) if values else SourceValue(ast.Constant(None))
		if name == "copy":
			return SourceValue(
				node,
				items={key: self.default_alias(value, base) for key, value in base.items.items()},
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
			item = EffectAnalyzer.default_alias(item, base)
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

	def stringify(self, value, seen, *, representation=False):
		name = "__repr__" if representation or "__str__" not in (value.items or {}) else "__str__"
		return (
			self.protocol(value, (name,), [], seen)
			if name in (value.items or {})
			else SourceValue(ast.Constant(""), scalar=True)
		)

	@staticmethod
	def literal_truth(value):
		if value.scalar and not value.uncertain and isinstance(value.expression, ast.Constant):
			return bool(value.expression.value)
		return None

	@staticmethod
	def operator_stem(operator):
		return {
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
		}.get(type(operator))

	@staticmethod
	def protocol_result(value):
		if not value.uncertain and value.modules == frozenset({"builtins.NotImplemented"}):
			return "fallback"
		if not value.uncertain and (
			value.scalar
			or value.instance
			or value.is_class
			or value.items is not None
			or value.helpers
			or isinstance(value.expression, ast.Constant)
		):
			return "handled"
		return "unknown"

	def ordered_protocols(self, left, right, direct, reflected, seen, *, binary=False):
		candidates = [(left, direct, right), (right, reflected, left)]
		if (
			binary
			and left.class_order
			and right.class_order
			and self.class_identity(left.class_order[0]) == self.class_identity(right.class_order[0])
		):
			candidates.pop()
		left_reflected = (left.items or {}).get(reflected)
		right_reflected = (right.items or {}).get(reflected)
		same_reflected = left_reflected is right_reflected or bool(
			left_reflected
			and right_reflected
			and left_reflected.helpers == right_reflected.helpers
			and left_reflected.modules == right_reflected.modules
			and not left_reflected.uncertain
			and not right_reflected.uncertain
		)
		if (
			(not binary or not same_reflected)
			and left.class_order
			and right.class_order
			and any(
				self.class_identity(left.class_order[0]) == self.class_identity(owner)
				for owner in right.class_order[1:]
			)
		):
			candidates.reverse()
		possible = []
		for receiver, name, argument in candidates:
			if name not in (receiver.items or {}):
				continue
			result = self.protocol(receiver, (name,), [argument], seen)
			status = self.protocol_result(result)
			if status == "fallback":
				continue
			possible.append(result)
			if status == "handled":
				break
		return (
			self.merge(possible)
			if possible
			else SourceValue(
				ast.Name(id="NotImplemented", ctx=ast.Load()),
				modules=frozenset({"builtins.NotImplemented"}),
				scalar=True,
			)
		)

	def arithmetic(self, left, right, operator, seen, *, inplace=False):
		stem = self.operator_stem(operator)
		if not stem:
			return SourceValue(ast.Constant(None), uncertain=True)
		if inplace and f"__i{stem}__" in (left.items or {}):
			result = self.protocol(left, (f"__i{stem}__",), [right], seen)
			status = self.protocol_result(result)
			if status == "handled":
				return result
			fallback = self.ordered_protocols(left, right, f"__{stem}__", f"__r{stem}__", seen, binary=True)
			return fallback if status == "fallback" else self.merge([result, fallback])
		return self.ordered_protocols(left, right, f"__{stem}__", f"__r{stem}__", seen, binary=True)

	def comparison(self, left, right, operator, seen):
		if isinstance(operator, (ast.In, ast.NotIn)):
			if "__contains__" in (right.items or {}):
				result = self.protocol(right, ("__contains__",), [left], seen)
			else:
				item = self.consume(right, seen)
				result = self.ordered_protocols(item, left, "__eq__", "__eq__", seen)
			truth = self.truth(result, seen)
			known = self.literal_truth(truth)
			if known is None:
				known = self.literal_truth(result)
			if known is not None and isinstance(operator, ast.NotIn):
				known = not known
			return SourceValue(
				ast.Constant(known) if known is not None else left.expression,
				scalar=True,
				uncertain=known is None,
			)
		methods = {
			ast.Eq: ("__eq__", "__eq__"),
			ast.NotEq: ("__ne__", "__ne__"),
			ast.Lt: ("__lt__", "__gt__"),
			ast.LtE: ("__le__", "__ge__"),
			ast.Gt: ("__gt__", "__lt__"),
			ast.GtE: ("__ge__", "__le__"),
		}
		if type(operator) not in methods:
			return SourceValue(ast.Constant(None), uncertain=True)
		result = self.ordered_protocols(left, right, *methods[type(operator)], seen)
		return replace(result, uncertain=True) if self.protocol_result(result) == "fallback" else result

	def protocol(self, value, names, arguments, seen):
		results = []
		for name in names:
			method = (value.items or {}).get(name)
			if method is None:
				continue
			location = next((helper.node for helper in method.helpers), value.expression)
			if not hasattr(location, "lineno"):
				location = ast.Constant(None, lineno=0, col_offset=0)
			if method.helpers:
				for helper in method.helpers:
					receiver = (
						value.class_order[0] if helper.descriptor == "class" and value.class_order else value
					)
					bound = () if helper.descriptor == "static" and name != "__new__" else (receiver,)
					callee = replace(
						method, helpers=(helper,), bound=bound, uncertain=method.uncertain or value.uncertain
					)
					results.append(self.callback(callee, arguments, location, seen))
			else:
				results.append(self.callback(replace(method, uncertain=True), arguments, location, seen))
		return self.merge(results) if results else SourceValue(ast.Constant(None))

	@staticmethod
	def class_identity(value):
		origins = frozenset(origin for origin in value.origins if isinstance(origin, ast.ClassDef))
		return origins if origins else id(value)

	def linearize(self, value, bases):
		sequences = [list(base.class_order) if base.class_order else [base] for base in bases] + [list(bases)]
		result = [value]
		while any(sequences):
			sequences = [sequence for sequence in sequences if sequence]
			candidate = next(
				(
					sequence[0]
					for sequence in sequences
					if all(
						self.class_identity(sequence[0])
						not in [self.class_identity(item) for item in other[1:]]
						for other in sequences
					)
				),
				None,
			)
			if candidate is None:
				# An unprovable MRO is unknown, not a different precedence order.
				value.uncertain = True
				result.extend(item for sequence in sequences for item in sequence)
				break
			result.append(candidate)
			for sequence in sequences:
				if self.class_identity(sequence[0]) == self.class_identity(candidate):
					sequence.pop(0)
		return tuple(result)

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
		# Ownership belongs to the captured objects, including aliases that predate
		# the definition. Copies retain only the identities they actually share.
		for value in defaults.values():
			if not value.scalar:
				for origin in self.reachable(value):
					self.default_owners.setdefault(origin, set()).add(node)
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
			base_values = (
				[self.value(base, scope) for base in node.bases] if isinstance(node, ast.ClassDef) else []
			)
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
			value = SourceValue(
				ast.Constant(None),
				helpers=helpers,
				is_class=isinstance(node, ast.ClassDef) and not replaced,
				origins=frozenset({node}),
				declared=local.values.copy() if isinstance(node, ast.ClassDef) else None,
				uncertain=any(base.uncertain for base in base_values),
			)
			if isinstance(node, ast.ClassDef):
				value.class_order = self.linearize(value, base_values)
				value.items = {}
				for owner in reversed(value.class_order):
					value.items.update(owner.declared if owner.declared is not None else owner.items or {})
			if isinstance(node, ast.ClassDef):
				for base_value in base_values:
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
			loaded = copy(node.target)
			loaded.ctx = ast.Load()
			self.expression(loaded, scope, seen)
			previous = self.value(loaded, scope)
			self.expression(node.value, scope, seen)
			addition = self.value(node.value, scope)
			result = self.arithmetic(previous, addition, node.op, seen, inplace=True)
			if self.protocol_result(result) != "fallback":
				self.bind(node.target, result, scope, seen)
				return False
			mutable = self.sequence_kind(previous) is ast.List or any(
				isinstance(origin, (ast.List, ast.Dict, ast.Set))
				or (
					isinstance(origin, ast.Call)
					and self.value(origin.func, scope).modules
					& {"builtins.list", "builtins.dict", "builtins.set"}
				)
				for origin in previous.origins
			)
			immutable = (
				previous.scalar
				or self.sequence_kind(previous) is ast.Tuple
				or (
					bool(previous.origins)
					and all(isinstance(origin, ast.Tuple) for origin in previous.origins)
				)
			)
			if mutable:
				self.emit(SourceEffect(node, mutation=True, shallow=True, origins=previous.origins))
			elif not immutable:
				self.emit(
					SourceEffect(
						node,
						opaque=True,
						origins=self.reachable(previous),
						argument_origins=self.exposed_origins(previous) | self.exposed_origins(addition),
					)
				)
			items = None
			if previous.items is not None and addition.items is not None:
				items = (
					dict(enumerate([*previous.items.values(), *addition.items.values()]))
					if isinstance(node.op, ast.Add)
					else {**previous.items, **addition.items}
				)
			value = replace(
				previous,
				origins=previous.origins if mutable else frozenset({node}),
				items=items,
				uncertain=True,
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
					(alias.name if alias.asname else alias.name.split(".")[0])
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
						origins=frozenset(
							{
								("import", module)
								if module
								else ("relative-import", node.level, node.module, alias.name)
							}
						),
						false_constant=module in {"typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"}
						and node.module not in self.changed_typing,
					),
					scope,
				)
		elif isinstance(node, (ast.With, ast.AsyncWith)):
			contexts = []
			asynchronous = isinstance(node, ast.AsyncWith)
			for item in node.items:
				self.expression(item.context_expr, scope, seen)
				context = self.value(item.context_expr, scope)
				entry = (
					self.consume(context, seen)
					if context.deferred and not asynchronous
					else self.protocol(context, ("__aenter__",) if asynchronous else ("__enter__",), [], seen)
				)
				if asynchronous:
					entry = self.consume(entry, seen, awaiting=True)
				contexts.append(context)
				if item.optional_vars:
					self.bind(item.optional_vars, entry, scope, seen)
			for statement in node.body:
				if self.visit(statement, scope, seen):
					break
			for context in reversed(contexts):
				exit_value = self.protocol(
					context,
					("__aexit__",) if asynchronous else ("__exit__",),
					[SourceValue(ast.Constant(None))] * 3,
					seen,
				)
				if asynchronous:
					self.consume(exit_value, seen, awaiting=True)
		elif isinstance(node, (ast.While, ast.Assert)):
			self.expression(node.test, scope, seen)
			self.truth(self.value(node.test, scope), seen)
			if isinstance(node, ast.While):
				self.branches([node.body, node.orelse], scope, seen)
		elif isinstance(node, ast.If):
			self.expression(node.test, scope, seen)
			self.truth(self.value(node.test, scope), seen)
			if self.value(node.test, scope).false_constant:
				for statement in node.orelse:
					if self.visit(statement, scope, seen):
						return True
			else:
				self.branches([node.body, node.orelse], scope, seen, include_initial=False)
		elif isinstance(node, (ast.For, ast.AsyncFor)):
			self.expression(node.iter, scope, seen)
			iterable = self.value(node.iter, scope)
			produced = (
				self.consume_async(iterable, seen)
				if isinstance(node, ast.AsyncFor)
				else self.consume(iterable, seen)
			)
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
		elif isinstance(node, (ast.Try, ast.TryStar)):
			# Handler expressions run only on exception paths and observe the try's state.
			scopes = []
			owner = scope
			while owner:
				scopes.append(owner)
				owner = owner.parent

			def snapshot():
				return [owner.values.copy() for owner in scopes]

			def restore(state):
				for owner, values in zip(scopes, state, strict=True):
					owner.values = values.copy()

			def combine(states):
				for index, owner in enumerate(scopes):
					keys = set().union(*(state[index] for state in states))
					owner.values = {
						key: self.merge(
							[state[index].get(key, SourceValue(ast.Constant(None))) for state in states]
						)
						for key in keys
					}

			exception_states = []
			terminal = None
			self.branch_depth += 1
			for statement in node.body:
				can_raise = isinstance(statement, ast.ClassDef) or any(
					isinstance(
						child,
						(
							ast.Call,
							ast.Raise,
							ast.Attribute,
							ast.Subscript,
							ast.BinOp,
							ast.Compare,
							ast.Await,
						),
					)
					or (isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load))
					for child in self.expression_nodes(statement)
				)
				if can_raise:
					exception_states.append(snapshot())
				if self.visit(statement, scope, seen):
					terminal = statement
					if can_raise:
						exception_states.append(snapshot())
					break
				if can_raise:
					exception_states.append(snapshot())
			self.branch_depth -= 1
			normal = snapshot()
			outcomes = []
			if not isinstance(terminal, ast.Raise):
				if terminal is None:
					for statement in node.orelse:
						if self.visit(statement, scope, seen):
							break
				outcomes.append(snapshot())
			if exception_states and node.handlers:
				combine(exception_states)
				handler_outcomes = []
				for handler in node.handlers:
					if handler.type is not None:
						self.expression(handler.type, scope, seen)
					unmatched = snapshot()
					self.branch_depth += 1
					for statement in handler.body:
						if self.visit(statement, scope, seen):
							break
					self.branch_depth -= 1
					handler_outcomes.append(snapshot())
					restore(unmatched)
					if handler.type is None:
						break
				outcomes.extend(handler_outcomes)
			if outcomes:
				combine(outcomes)
			else:
				restore(normal)
			for statement in node.finalbody:
				if self.visit(statement, scope, seen):
					break
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
