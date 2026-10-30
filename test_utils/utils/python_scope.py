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
		self.return_values = []

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

	def value(self, node, scope):
		if node in self.call_values:
			return self.call_values[node]
		if isinstance(node, ast.Name):
			return scope.get(node.id)
		if isinstance(node, ast.Attribute):
			base = self.value(node.value, scope)
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
		if isinstance(node, (ast.List, ast.Tuple)):
			items = {i: self.value(item, scope) for i, item in enumerate(node.elts)}
			expression = type(node)(elts=[value.expression for value in items.values()], ctx=ast.Load())
			return SourceValue(ast.copy_location(expression, node), items=items, origins=frozenset({node}))
		if isinstance(node, ast.Dict) and all(isinstance(key, ast.Constant) for key in node.keys):
			items = {
				key.value: self.value(item, scope) for key, item in zip(node.keys, node.values, strict=True)
			}
			return SourceValue(
				ast.copy_location(
					ast.Dict(keys=node.keys, values=[item.expression for item in items.values()]), node
				),
				items=items,
				origins=frozenset({node}),
			)
		if isinstance(node, ast.NamedExpr):
			return self.value(node.value, scope)
		if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
			base = self.value(node.value, scope)
			try:
				index = ast.literal_eval(node.slice)
				if base.items is not None and index in base.items:
					return base.items[index]
			except ValueError, TypeError:
				pass
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
		return SourceValue(expression)

	def scan(self, statement):
		self.effects = []
		self.visit(statement, self.scope, frozenset())
		return self.effects

	def expression(self, node, scope, seen):
		if isinstance(node, ast.YieldFrom):
			self.expression(node.value, scope, seen)
			self.consume(self.value(node.value, scope), seen)
			return
		if isinstance(node, ast.NamedExpr):
			self.visit(node, scope, seen)
			return
		if isinstance(node, ast.Await):
			self.expression(node.value, scope, seen)
			self.consume(self.value(node.value, scope), seen)
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
				values = list(iterable.items.values()) if iterable.items else []
				value = self.merge(values) if values else SourceValue(ast.Constant(None))
				self.bind(generator.target, value, local, seen)
				for condition in generator.ifs:
					self.expression(condition, local, seen)
			for element in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
				self.expression(element, local, seen)
			return
		if isinstance(node, ast.Lambda):
			for item in definition_expressions(node):
				self.expression(item, scope, seen)
			return
		for child in ast.iter_child_nodes(node):
			if isinstance(child, ast.expr):
				self.expression(child, scope, seen)
			elif isinstance(child, ast.keyword):
				self.expression(child.value, scope, seen)
		if not isinstance(node, ast.Call):
			return
		callee = self.value(node.func, scope)
		arguments = []
		for item in node.args:
			value = self.value(item.value if isinstance(item, ast.Starred) else item, scope)
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
		namespace = bool(
			callee.modules & {f"builtins.{name}" for name in ("globals", "locals", "exec", "eval")}
		)
		if "builtins.vars" in callee.modules:
			namespace |= len(arguments) != 1 or not arguments[0].is_class or bool(keywords)
		changed = set()
		if callee.modules & {"builtins.setattr", "builtins.delattr"} and arguments:
			attribute = node.args[1] if len(node.args) > 1 else None
			if not isinstance(attribute, ast.Constant) or attribute.value == "TYPE_CHECKING":
				changed.update(arguments[0].modules & {"typing", "typing_extensions"})
		references = self.reachable(callee)
		consumers = {
			f"builtins.{name}"
			for name in ("list", "tuple", "set", "dict", "next", "sum", "any", "all", "sorted")
		} | {"asyncio.run"}
		readers = {
			f"builtins.{name}" for name in ("iter", "print", "id", "type", "repr", "str", "bool", "len")
		}
		if not callee.helpers and not (callee.modules & (consumers | readers)):
			references |= frozenset().union(
				*(self.reachable(item) for item in [*arguments, *keywords.values()] if not item.deferred)
			)
		# Reading vars(cls) does not itself mutate the class; calls on its result may.
		if "builtins.vars" in callee.modules and not namespace:
			references = frozenset()
		self.emit(
			SourceEffect(
				self.value(node, scope).expression,
				namespace=namespace,
				typing_modules=frozenset(changed),
				origins=references,
			)
		)
		results = []
		for helper in callee.helpers:
			if helper.deferred:
				results.append(SourceValue(node, deferred=(DeferredExecution(helper, arguments, keywords),)))
			else:
				results.append(self.invoke(helper, arguments, keywords, seen))
		if callee.modules & consumers or (not callee.helpers and not callee.modules & readers):
			for argument in arguments:
				self.consume(argument, seen)
		if "builtins.iter" in callee.modules and arguments:
			results.append(arguments[0])
		if "builtins.vars" in callee.modules and arguments:
			results.append(SourceValue(node, origins=self.reachable(arguments[0])))
		if isinstance(node.func, ast.Attribute):
			iterator = self.value(node.func.value, scope)
			if node.func.attr in {"__next__", "send", "throw"}:
				self.consume(iterator, seen)
			elif node.func.attr in self.ASYNC_ADVANCES and iterator.deferred:
				results.append(
					SourceValue(node, deferred=(DeferredExecution(None, [], {}, advance=iterator),))
				)
		if results:
			self.call_values[node] = self.merge(results)

	def consume(self, value, seen):
		for deferred in value.deferred:
			if id(deferred) in seen:
				continue
			if deferred.advance is not None:
				self.consume(deferred.advance, seen | {id(deferred)})
			elif deferred.helper:
				self.invoke(deferred.helper, deferred.arguments, deferred.keywords, seen | {id(deferred)})
			else:
				local = SourceScope(deferred.scope, kind="function")
				for index, generator in enumerate(deferred.expression.generators):
					if index:
						self.expression(generator.iter, local, seen)
					iterable = self.value(generator.iter, local) if index else deferred.outer_iterable
					self.consume(iterable, seen | {id(deferred)})
					values = list(iterable.items.values()) if iterable.items else []
					self.bind(
						generator.target,
						self.merge(values) if values else SourceValue(ast.Constant(None)),
						local,
						seen,
					)
					for condition in generator.ifs:
						self.expression(condition, local, seen)
				self.expression(deferred.expression.elt, local, seen)

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

	def invoke(self, helper, arguments, keywords, seen):
		function, closure = helper.node, helper.closure
		if id(function) in seen:
			return SourceValue(ast.Constant(None))
		local = SourceScope(closure, kind="function", body=function.body)
		params = [*function.args.posonlyargs, *function.args.args]
		for parameter in [*params, *function.args.kwonlyargs]:
			local.values[parameter.arg] = SourceValue(ast.Constant(None))
		local.values.update(helper.defaults)
		for parameter, argument in zip(params, arguments, strict=False):
			local.values[parameter.arg] = argument
		local.values.update({name: value for name, value in keywords.items() if name in local.values})
		if function.args.vararg:
			items = dict(enumerate(arguments[len(params) :]))
			local.values[function.args.vararg.arg] = SourceValue(
				ast.Tuple(elts=[item.expression for item in items.values()], ctx=ast.Load()), items=items
			)
		if function.args.kwarg:
			items = {name: value for name, value in keywords.items() if name not in local.values}
			local.values[function.args.kwarg.arg] = SourceValue(
				ast.Dict(
					keys=[ast.Constant(name) for name in items],
					values=[item.expression for item in items.values()],
				),
				items=items,
			)
		self.return_values.append([])
		for statement in function.body:
			if self.visit(statement, local, seen | {id(function)}):
				break
		results = self.return_values.pop()
		if not self.always_returns(function.body) or not results:
			results.append(SourceValue(ast.Constant(None)))
		return self.merge(results)

	def bind(self, target, value, scope, seen=frozenset(), *, anchor=True):
		if isinstance(target, ast.Name):
			if scope.set(target.id, value, anchor=anchor):
				self.emit(SourceEffect(target, bindings=frozenset({target.id})))
		elif isinstance(target, (ast.Tuple, ast.List)):
			for index, item in enumerate(target.elts):
				self.bind(
					item,
					value.items.get(index, SourceValue(ast.Constant(None)))
					if value.items
					else SourceValue(ast.Constant(None)),
					scope,
					seen,
					anchor=anchor,
				)
		else:
			self.expression(target, scope, seen)
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
		for block in blocks:
			for item, values in zip(scopes, before, strict=True):
				item.values = values.copy()
			for statement in block:
				if self.visit(statement, scope, seen):
					break
			outcomes.append([item.values.copy() for item in scopes])
		for index, owner in enumerate(scopes):
			owner.values = {}
			for name in set().union(*(outcome[index] for outcome in outcomes)):
				values = [outcome[index].get(name, SourceValue(ast.Constant(None))) for outcome in outcomes]
				owner.values[name] = self.merge(values)

	@staticmethod
	def merge(values):
		if all(value is values[0] for value in values):
			return values[0]
		return SourceValue(
			ast.Tuple(elts=[value.expression for value in values], ctx=ast.Load()),
			modules=frozenset().union(*(value.modules for value in values)),
			helpers=tuple({id(helper): helper for value in values for helper in value.helpers}.values()),
			origins=frozenset().union(*(value.origins for value in values)),
			deferred=tuple({id(item): item for value in values for item in value.deferred}.values()),
			false_constant=all(value.false_constant for value in values),
			uncertain=any(value.uncertain for value in values)
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
				callee = self.value(decorator.func if isinstance(decorator, ast.Call) else decorator, scope)
				for helper in callee.helpers:
					function = helper.node
					identity = False
					start = len(self.effects)
					if not isinstance(decorator, ast.Call) and not helper.deferred:
						result = self.invoke(helper, [argument], {}, seen)
						identity = (
							self.always_returns(function.body)
							and not result.uncertain
							and result.origins == argument.origins
						)
					replaced |= (
						not identity
						or isinstance(decorator, ast.Call)
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
			closure = scope
			while closure.kind == "class":
				closure = closure.parent
			helpers = ()
			if isinstance(node, FUNCTION_NODES) and not replaced:
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
						for parameter, default in zip(
							node.args.kwonlyargs, node.args.kw_defaults, strict=True
						)
						if default is not None
					}
				)
				deferred = isinstance(node, ast.AsyncFunctionDef) or any(
					isinstance(child, (ast.Yield, ast.YieldFrom))
					for statement in scope_statements(node.body)
					for child in self.expression_nodes(statement)
				)
				helpers = (SourceFunction(node, closure, defaults, deferred),)
			value = SourceValue(
				ast.Constant(None),
				helpers=helpers,
				is_class=isinstance(node, ast.ClassDef) and not replaced,
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
		elif isinstance(node, (ast.AugAssign, ast.Delete)):
			if isinstance(node, ast.AugAssign):
				self.expression(node.value, scope, seen)
			for target in node.targets if isinstance(node, ast.Delete) else [node.target]:
				if isinstance(target, ast.Name):
					self.emit(
						SourceEffect(
							self.value(target, scope).expression,
							mutation=isinstance(node, ast.AugAssign) and scope is not scope.root,
							origins=self.reachable(self.value(target, scope))
							if isinstance(node, ast.AugAssign) and scope is not scope.root
							else frozenset(),
						)
					)
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
		elif isinstance(node, ast.If):
			self.expression(node.test, scope, seen)
			if self.value(node.test, scope).false_constant:
				for statement in node.orelse:
					if self.visit(statement, scope, seen):
						return True
			else:
				self.branches([node.body, node.orelse], scope, seen, include_initial=False)
		elif isinstance(node, (ast.For, ast.AsyncFor)):
			self.expression(node.iter, scope, seen)
			iterable = self.value(node.iter, scope)
			self.consume(iterable, seen)
			values = list(iterable.items.values()) if iterable.items else []
			self.bind(
				node.target,
				self.merge(values) if values else SourceValue(ast.Constant(None)),
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
