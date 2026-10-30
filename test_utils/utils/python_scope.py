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


@dataclass
class SourceEffect:
	node: ast.AST
	bindings: frozenset[str] = frozenset()
	namespace: bool = False
	typing_modules: frozenset[str] = frozenset()
	replaced_definition: str | None = None
	mutation: bool = False


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
		if owner.parent:
			return owner.parent.get(name)
		return SourceValue(ast.Name(id=name, ctx=ast.Load()))

	def set(self, name, value, *, anchor=True):
		owner = self.binding_scope(name)
		if owner is owner.root and anchor:
			value = replace(value, expression=ast.Name(id=name, ctx=ast.Load()))
		owner.values[name] = value
		return owner is self.root and self is not self.root


class EffectAnalyzer:
	"""Conservative lexical analysis of executed syntax, never importing an app."""

	BUILTINS = ("vars", "globals", "locals", "exec", "eval", "setattr", "delattr")

	def __init__(self):
		self.scope = SourceScope()
		for name in self.BUILTINS:
			self.scope.values[name] = SourceValue(
				ast.Name(id=name, ctx=ast.Load()), modules=frozenset({f"builtins.{name}"})
			)
		self.effects = []

	def value(self, node, scope):
		if isinstance(node, ast.Name):
			return scope.get(node.id)
		if isinstance(node, ast.Attribute):
			base = self.value(node.value, scope)
			modules = frozenset(f"{name}.{node.attr}" for name in base.modules)
			return SourceValue(
				ast.copy_location(ast.Attribute(value=base.expression, attr=node.attr, ctx=node.ctx), node),
				modules=modules,
			)
		if isinstance(node, (ast.List, ast.Tuple)):
			items = {i: self.value(item, scope) for i, item in enumerate(node.elts)}
			expression = type(node)(elts=[value.expression for value in items.values()], ctx=ast.Load())
			return SourceValue(ast.copy_location(expression, node), items=items)
		if isinstance(node, ast.Dict) and all(isinstance(key, ast.Constant) for key in node.keys):
			items = {
				key.value: self.value(item, scope) for key, item in zip(node.keys, node.values, strict=True)
			}
			return SourceValue(
				ast.copy_location(
					ast.Dict(keys=node.keys, values=[item.expression for item in items.values()]), node
				),
				items=items,
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
		if isinstance(node, ast.NamedExpr):
			self.visit(node, scope, seen)
			return
		if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
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
		self.effects.append(
			SourceEffect(
				self.value(node, scope).expression, namespace=namespace, typing_modules=frozenset(changed)
			)
		)
		for function, closure in callee.helpers:
			self.invoke(function, closure, arguments, keywords, seen)

	def invoke(self, function, closure, arguments, keywords, seen):
		if id(function) in seen:
			return
		local = SourceScope(closure, kind="function", body=function.body)
		params = [*function.args.posonlyargs, *function.args.args]
		for parameter in [*params, *function.args.kwonlyargs]:
			local.values[parameter.arg] = SourceValue(ast.Constant(None))
		for parameter, default in zip(
			params[-len(function.args.defaults) :] if function.args.defaults else [],
			function.args.defaults,
			strict=True,
		):
			local.values[parameter.arg] = self.value(default, closure)
		for parameter, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True):
			if default is not None:
				local.values[parameter.arg] = self.value(default, closure)
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
		for statement in function.body:
			if self.visit(statement, local, seen | {id(function)}):
				break

	def bind(self, target, value, scope, seen=frozenset(), *, anchor=True):
		if isinstance(target, ast.Name):
			if scope.set(target.id, value, anchor=anchor):
				self.effects.append(SourceEffect(target, bindings=frozenset({target.id})))
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
			self.effects.append(
				SourceEffect(
					self.value(target, scope).expression,
					typing_modules=modules & {"typing", "typing_extensions"},
				)
			)

	def branches(self, blocks, scope, seen):
		scopes = []
		owner = scope
		while owner:
			scopes.append(owner)
			owner = owner.parent
		before = [item.values.copy() for item in scopes]
		outcomes = [before]
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
			helpers=tuple(
				{
					(id(fn), id(closure)): (fn, closure) for value in values for fn, closure in value.helpers
				}.values()
			),
			is_class=all(value.is_class for value in values),
		)

	def visit(self, node, scope, seen):
		if isinstance(node, ast.AnnAssign) and node.value is None:
			return False
		if isinstance(node, DEFINITION_NODES):
			for expression in definition_expressions(node):
				self.expression(expression, scope, seen)
			replaced = False
			for decorator in node.decorator_list:
				callee = self.value(decorator.func if isinstance(decorator, ast.Call) else decorator, scope)
				for function, closure in callee.helpers:
					params = [*function.args.posonlyargs, *function.args.args]
					returns = [
						item for item in scope_statements(function.body) if isinstance(item, ast.Return)
					]
					identity = bool(params and returns) and all(
						isinstance(item.value, ast.Name) and item.value.id == params[0].arg
						for item in returns
					)
					if identity:
						parameter = params[0].arg
						identity = parameter not in scope_bindings(function.body)
						for item in ast.walk(function):
							if isinstance(item, (ast.Attribute, ast.Subscript)) and isinstance(
								item.ctx, (ast.Store, ast.Del)
							):
								identity &= not any(
									isinstance(child, ast.Name) and child.id == parameter
									for child in ast.walk(item)
								)
							elif isinstance(item, ast.Call):
								identity &= not any(
									isinstance(arg, ast.Name) and arg.id == parameter for arg in item.args
								)
					replaced |= not identity or isinstance(decorator, ast.Call)
					if not isinstance(decorator, ast.Call):
						self.invoke(
							function,
							closure,
							[SourceValue(ast.Constant(None), is_class=isinstance(node, ast.ClassDef))],
							{},
							seen,
						)
			if isinstance(node, ast.ClassDef):
				local = SourceScope(scope, kind="class", body=node.body)
				for statement in node.body:
					self.visit(statement, local, seen)
			closure = scope
			while closure.kind == "class":
				closure = closure.parent
			value = SourceValue(
				ast.Constant(None),
				helpers=((node, closure),) if isinstance(node, FUNCTION_NODES) and not replaced else (),
				is_class=isinstance(node, ast.ClassDef) and not replaced,
			)
			self.bind(ast.copy_location(ast.Name(id=node.name, ctx=ast.Store()), node), value, scope)
			if replaced:
				self.effects.append(SourceEffect(node, replaced_definition=node.name))
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
					self.effects.append(
						SourceEffect(
							self.value(target, scope).expression,
							mutation=isinstance(node, ast.AugAssign) and scope is not scope.root,
						)
					)
				self.bind(target, SourceValue(ast.Constant(None)), scope, seen)
		elif isinstance(node, (ast.Import, ast.ImportFrom)):
			if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
				# A wildcard may replace an earlier class with a module or another object.
				for name, value in scope.values.items():
					scope.values[name] = replace(value, is_class=False)
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
					SourceValue(ast.Constant(None), modules=frozenset({module}) if module else frozenset()),
					scope,
				)
		elif isinstance(node, ast.If):
			self.expression(node.test, scope, seen)
			self.branches([node.body, node.orelse], scope, seen)
		elif isinstance(node, (ast.For, ast.AsyncFor)):
			self.expression(node.iter, scope, seen)
			iterable = self.value(node.iter, scope)
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
