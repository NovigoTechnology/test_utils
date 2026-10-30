"""Scope-aware Python source inspection without importing application code."""

import ast
from collections.abc import Iterator
from dataclasses import dataclass

FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITION_NODES = (*FUNCTION_NODES, ast.ClassDef)


def scope_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
	"""Include conditional definitions/imports, but never descend into another scope."""
	for node in body:
		yield node
		if not isinstance(node, DEFINITION_NODES):
			for _, value in ast.iter_fields(node):
				if isinstance(value, list):
					for child in value:
						if isinstance(child, ast.stmt):
							yield from scope_statements([child])
						elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
							yield from scope_statements(child.body)


def overload_names(body):
	names = {"overload", "typing.overload", "typing_extensions.overload"}
	for item in scope_statements(body):
		if isinstance(item, ast.ImportFrom) and item.module in {"typing", "typing_extensions"}:
			for alias in item.names:
				if alias.name == "overload":
					names.add(alias.asname or alias.name)
		elif isinstance(item, ast.Import):
			for alias in item.names:
				if alias.name in {"typing", "typing_extensions"}:
					names.add(f"{alias.asname or alias.name}.overload")
	return names


def decorator_name(node):
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		return f"{decorator_name(node.value)}.{node.attr}"
	return ""


def definition_expressions(node):
	"""Expressions evaluated when a function, lambda or class is defined."""
	if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
		yield from node.decorator_list
	if isinstance(node, ast.ClassDef):
		yield from node.bases
		yield from (keyword.value for keyword in node.keywords)
	else:
		yield from node.args.defaults
		yield from (value for value in node.args.kw_defaults if value is not None)


def executable_nodes(node):
	"""Walk definition-time expressions and class bodies, excluding deferred function bodies."""
	yield node
	if isinstance(node, (*DEFINITION_NODES, ast.Lambda)):
		children = list(definition_expressions(node))
		if isinstance(node, ast.ClassDef):
			children.extend(node.body)
	else:
		children = ast.iter_child_nodes(node)
	for child in children:
		yield from executable_nodes(child)


def scope_bindings(body):
	"""All declarations of each name, without confusing nested lexical scopes."""
	bindings = {}

	def add(name, node):
		if name:
			bindings.setdefault(name, []).append(node)

	def visit(node):
		if isinstance(node, DEFINITION_NODES):
			add(node.name, node)
			for expression in definition_expressions(node):
				visit(expression)
			return
		if isinstance(node, ast.Lambda):
			for expression in definition_expressions(node):
				visit(expression)
			return
		if isinstance(node, ast.AnnAssign) and node.value is None:
			return
		if isinstance(node, (ast.Assign, ast.AnnAssign)):
			targets = node.targets if isinstance(node, ast.Assign) else [node.target]
			for target in targets:
				for name in ast.walk(target):
					if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
						add(name.id, node)
			visit(node.value)
			return
		if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
			add(node.id, node)
		elif isinstance(node, (ast.Import, ast.ImportFrom)):
			for alias in node.names:
				add(
					alias.asname
					or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name),
					alias,
				)
		elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
			add(node.name, node)
		elif isinstance(node, ast.MatchMapping):
			add(node.rest, node)
		if isinstance(node, ast.comprehension):
			# Comprehension loop variables belong to the comprehension's scope.
			visit(node.iter)
			for condition in node.ifs:
				visit(condition)
			return
		for child in ast.iter_child_nodes(node):
			visit(child)

	for statement in body:
		visit(statement)
	return bindings


def static_attribute(bindings):
	"""Distinguish plain class data from potentially callable assigned attributes."""
	for node in bindings:
		if not isinstance(node, (ast.Assign, ast.AnnAssign)):
			return False
		try:
			ast.literal_eval(node.value)
		except ValueError, TypeError, SyntaxError:
			return False
	return bool(bindings)


class AmbiguousBinding(ValueError):
	"""A declaration cannot be proven to be the binding active at runtime."""


@dataclass
class Binding:
	node: ast.AST
	statement: ast.stmt
	conditional: bool = False


def runtime_body(body):
	"""Exclude proven typing.TYPE_CHECKING blocks, whose imports are not runtime exports."""
	typing_names = set()
	checking_names = set()
	for statement in body:
		guard = isinstance(statement, ast.If) and (
			(isinstance(statement.test, ast.Name) and statement.test.id in checking_names)
			or (
				isinstance(statement.test, ast.Attribute)
				and isinstance(statement.test.value, ast.Name)
				and statement.test.value.id in typing_names
				and statement.test.attr == "TYPE_CHECKING"
			)
		)
		statements = statement.orelse if guard else [statement]
		for active in statements:
			bindings = scope_bindings([active])
			if "*" in bindings:
				typing_names.clear()
				checking_names.clear()
			typing_names.difference_update(bindings)
			checking_names.difference_update(bindings)
			if isinstance(active, ast.Import):
				for alias in active.names:
					name = alias.asname or alias.name.split(".")[0]
					typing_names.discard(name)
					checking_names.discard(name)
					if alias.name in {"typing", "typing_extensions"}:
						typing_names.add(name)
			elif (
				isinstance(active, ast.ImportFrom)
				and active.level == 0
				and active.module in {"typing", "typing_extensions"}
			):
				for alias in active.names:
					name = alias.asname or alias.name
					typing_names.discard(name)
					checking_names.discard(name)
					if alias.name == "TYPE_CHECKING":
						checking_names.add(name)
			yield active


def ordered_bindings(body, name):
	"""Keep declaration order and the statement responsible for each binding."""
	for statement in runtime_body(body):
		bindings = scope_bindings([statement])
		for node in sorted([*bindings.get(name, []), *bindings.get("*", [])], key=lambda item: item.lineno):
			direct = node is statement or (
				isinstance(statement, (ast.Import, ast.ImportFrom)) and node in statement.names
			)
			responsible = statement
			if isinstance(node, ast.alias):
				responsible = next(
					child
					for child in ast.walk(statement)
					if isinstance(child, (ast.Import, ast.ImportFrom)) and node in child.names
				)
			yield Binding(node, responsible, not direct)


def select_binding(body, name, *, wildcard=None, overloads=()):
	"""Select the last proven declaration; earlier wildcards cannot replace it."""
	for binding in reversed(list(ordered_bindings(body, name))):
		node = binding.node
		if isinstance(node, ast.alias) and node.name == "*":
			if wildcard is None:
				raise AmbiguousBinding(
					f"wildcard import at line {binding.statement.lineno} may replace {name}"
				)
			value = wildcard(binding.statement, name)
			if value is None:
				continue
			if binding.conditional:
				raise AmbiguousBinding(
					f"conditional wildcard import at line {binding.statement.lineno} may replace {name}"
				)
			return value
		if binding.conditional:
			raise AmbiguousBinding(f"conditional declaration of {name} at line {node.lineno}")
		if isinstance(node, FUNCTION_NODES):
			decorators = {decorator_name(item) for item in node.decorator_list}
			if decorators.intersection(overloads):
				continue
			if decorators.intersection({f"{name}.setter", f"{name}.getter", f"{name}.deleter"}):
				raise AmbiguousBinding(f"descriptor {name} has multiple accessors at line {node.lineno}")
		return binding
	return None


def called_export_effects(node, functions, seen=frozenset(), *, indirect=False):
	"""Inspect executed helpers without treating every deferred body as executed."""
	for child in executable_nodes(node):
		yield child, indirect
		if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
			function = functions.get(child.func.id)
			if function is not None and id(function) not in seen:
				for statement in function.body:
					yield from called_export_effects(
						statement, functions, seen | {id(function)}, indirect=True
					)


def static_exports(body, *, wildcard=None):
	"""Return a proven __all__, or None for ordinary public-name exports."""
	unknown = object()
	values = {}
	invalid = set()
	functions = {}
	last_reset = max(
		(
			statement.lineno
			for statement in body
			if isinstance(statement, (ast.Assign, ast.AnnAssign)) and "__all__" in scope_bindings([statement])
		),
		default=0,
	)

	def literal(node):
		if isinstance(node, ast.Name):
			value = values.get(node.id, unknown)
			if value is unknown or id(value) in invalid:
				raise ValueError
			return value
		if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
			return literal(node.left) + literal(node.right)
		if isinstance(node, (ast.List, ast.Tuple)):
			items = []
			for item in node.elts:
				if isinstance(item, ast.Starred):
					items.extend(literal(item.value))
				else:
					items.append(literal(item))
			return tuple(items) if isinstance(node, ast.Tuple) else items
		return ast.literal_eval(node)

	for statement in body:
		if isinstance(statement, ast.ImportFrom) and any(alias.name == "*" for alias in statement.names):
			# Earlier imports may rebind constant aliases used to build a later __all__.
			for name in values.keys() - {"__all__"}:
				values[name] = unknown
			if statement.lineno < last_reset:
				continue
			if wildcard is None or wildcard(statement):
				values["__all__"] = unknown
			continue
		for node, indirect in called_export_effects(statement, functions):
			if indirect and isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
				values[node.id] = unknown
			if isinstance(node, ast.Call) or (
				isinstance(node, (ast.Subscript, ast.Attribute))
				and isinstance(node.ctx, (ast.Store, ast.Del))
			):
				for child in ast.walk(node):
					if isinstance(child, ast.Name) and isinstance(values.get(child.id), (list, tuple)):
						invalid.add(id(values[child.id]))
		if isinstance(statement, (ast.Assign, ast.AnnAssign)):
			targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
			names = [target.id for target in targets if isinstance(target, ast.Name)]
			try:
				value = literal(statement.value)
			except ValueError, TypeError, KeyError, SyntaxError:
				value = unknown
			values.update(dict.fromkeys(names, value))
			for name in scope_bindings([statement]).keys() - set(names):
				values[name] = unknown
		elif isinstance(statement, ast.AugAssign) and isinstance(statement.target, ast.Name):
			name = statement.target.id
			try:
				if not isinstance(statement.op, ast.Add):
					raise ValueError
				value = literal(statement.target)
				addition = literal(statement.value)
				if isinstance(value, list):
					value += addition
				else:
					values[name] = value + addition
			except ValueError, TypeError, KeyError:
				values[name] = unknown
		else:
			for name in scope_bindings([statement]):
				values[name] = unknown
		alias = (
			functions.get(statement.value.id)
			if (isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Name))
			else None
		)
		for name in scope_bindings([statement]):
			functions.pop(name, None)
		if isinstance(statement, FUNCTION_NODES):
			functions[statement.name] = statement
		elif alias is not None:
			for target in statement.targets:
				if isinstance(target, ast.Name):
					functions[target.id] = alias
	if "__all__" not in values:
		return None
	value = values["__all__"]
	if (
		not isinstance(value, (tuple, list))
		or id(value) in invalid
		or not all(isinstance(item, str) for item in value)
	):
		raise AmbiguousBinding("dynamic or invalid __all__ exports")
	return set(value)


def find_definition(tree: ast.Module | ast.ClassDef, qualified_name: str):
	parts = qualified_name.split(".")
	node = tree
	decorators = set()
	for part in parts:
		if not isinstance(node, (ast.Module, ast.ClassDef)):
			return None
		decorators.update(overload_names(node.body))
		try:
			binding = select_binding(node.body, part, overloads=decorators)
		except AmbiguousBinding:
			return None
		if binding is None or not isinstance(binding.node, DEFINITION_NODES):
			return None
		node = binding.node
	return node


def find_function(tree: ast.Module, name: str):
	"""Find the concrete implementation; ambiguous scopes and overload-only stubs fail."""
	node = find_definition(tree, name)
	if node is None and "." not in name:
		bindings = scope_bindings(tree.body)
		if name in bindings or "*" in bindings:
			return None
		candidates = []

		def collect(scope, prefix=""):
			if any(
				isinstance(item, FUNCTION_NODES) and item.name == name
				for item in scope_statements(scope.body)
			):
				candidates.append(find_definition(tree, prefix + name))
			for item in scope_statements(scope.body):
				if isinstance(item, ast.ClassDef):
					collect(item, f"{prefix}{item.name}.")

		collect(tree)
		if len(candidates) == 1:
			node = candidates[0]
	if not isinstance(node, FUNCTION_NODES):
		return None
	return node


def extract_function(source: str, name: str) -> str | None:
	"""Resolve a qualified method, or a unique legacy unqualified method name."""
	try:
		tree = ast.parse(source)
	except SyntaxError:
		return None
	node = find_function(tree, name)
	if node is None:
		return None
	start = min([node.lineno, *(dec.lineno for dec in node.decorator_list)])
	return "\n".join(source.splitlines()[start - 1 : node.end_lineno])
