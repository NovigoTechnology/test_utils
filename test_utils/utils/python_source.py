"""Scope-aware Python source inspection without importing application code."""

import ast
from collections.abc import Callable, Iterator
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


@dataclass(frozen=True)
class Binding:
	node: ast.AST
	statement: ast.stmt


class AmbiguousBinding(ValueError):
	def __init__(self, statement: ast.stmt, reason: str):
		self.statement = statement
		super().__init__(reason)


def runtime_statements(body: list[ast.stmt]) -> list[ast.stmt]:
	"""Omit proven typing.TYPE_CHECKING branches; leave other conditions unresolved."""
	guards = set()
	result = []

	def collect(statements):
		for statement in statements:
			if isinstance(statement, ast.If) and decorator_name(statement.test) in guards:
				collect(statement.orelse)
				continue
			result.append(statement)
			bindings = scope_bindings([statement])
			guards.difference_update(
				guard for guard in list(guards) if "*" in bindings or guard.split(".", 1)[0] in bindings
			)
			guards.difference_update(
				decorator_name(node)
				for node in executable_nodes(statement)
				if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del))
			)
			if not isinstance(statement, (ast.Import, ast.ImportFrom)):
				continue
			for alias in statement.names:
				local = alias.asname or (
					alias.name.split(".")[0] if isinstance(statement, ast.Import) else alias.name
				)
				guards.difference_update(guard for guard in list(guards) if guard.split(".", 1)[0] == local)
				if isinstance(statement, ast.Import) and alias.name in {"typing", "typing_extensions"}:
					guards.add(f"{local}.TYPE_CHECKING")
				elif (
					isinstance(statement, ast.ImportFrom)
					and not statement.level
					and statement.module in {"typing", "typing_extensions"}
					and alias.name == "TYPE_CHECKING"
				):
					guards.add(local)

	collect(body)
	return result


def select_binding(
	body: list[ast.stmt],
	name: str,
	*,
	decorators: set[str] | None = None,
	wildcard: Callable[[ast.ImportFrom, str], bool] | None = None,
) -> Binding | None:
	"""Select a declaration by source order, failing closed on unsupported bindings.

	The optional callback supplies filesystem knowledge about wildcard exports. This
	is declaration analysis, not evaluation of decorators or arbitrary Python code.
	"""
	body = runtime_statements(body)
	decorators = (decorators or set()) | overload_names(body)
	for statement in reversed(body):
		bindings = scope_bindings([statement])
		matches = bindings.get(name, [])
		if isinstance(statement, DEFINITION_NODES) and statement.name == name:
			if isinstance(statement, FUNCTION_NODES) and any(
				decorator_name(dec) in decorators for dec in statement.decorator_list
			):
				raise AmbiguousBinding(statement, f"overload-only declaration of {name!r}")
			# Getter/setter pairs do not denote a single concrete callable.
			if isinstance(statement, FUNCTION_NODES) and any(
				isinstance(dec, ast.Attribute) and dec.attr in {"getter", "setter", "deleter"}
				for dec in statement.decorator_list
			):
				raise AmbiguousBinding(statement, f"descriptor {name!r} has no single implementation")
			if matches == [statement]:
				return Binding(statement, statement)
		elif isinstance(statement, (ast.Import, ast.ImportFrom)):
			if matches:
				return Binding(matches[-1], statement)
			if isinstance(statement, ast.ImportFrom) and "*" in bindings:
				if wildcard is None:
					raise AmbiguousBinding(statement, "wildcard exports are unavailable")
				if wildcard(statement, name):
					return Binding(statement.names[0], statement)
				continue
		if matches or "*" in bindings:
			imports = [
				node
				for node in scope_statements([statement])
				if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
			]
			if not matches and wildcard and not any(wildcard(node, name) for node in imports):
				continue
			responsible = next(
				iter(imports),
				statement,
			)
			raise AmbiguousBinding(responsible, f"conditional or unsupported binding of {name!r}")
	return None


def find_definition(tree: ast.Module | ast.ClassDef, qualified_name: str):
	parts = qualified_name.split(".")
	node = tree
	decorators = set()
	for part in parts:
		if not isinstance(node, (ast.Module, ast.ClassDef)):
			return None
		decorators.update(overload_names(node.body))
		try:
			binding = select_binding(node.body, part, decorators=decorators)
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
