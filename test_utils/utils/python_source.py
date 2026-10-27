"""Scope-aware Python source inspection without importing application code."""

import ast
from collections.abc import Iterator

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


def find_definition(tree: ast.Module | ast.ClassDef, qualified_name: str):
	parts = qualified_name.split(".")
	node = tree
	decorators = set()
	for part in parts:
		if not isinstance(node, (ast.Module, ast.ClassDef)):
			return None
		decorators.update(overload_names(node.body))
		matches = scope_bindings(node.body).get(part, [])
		if any(not isinstance(item, DEFINITION_NODES) for item in matches):
			return None
		concrete = [
			item
			for item in matches
			if not isinstance(item, FUNCTION_NODES)
			or not any(decorator_name(dec) in decorators for dec in item.decorator_list)
		]
		if len(concrete) != 1:
			return None
		node = concrete[0]
	return node


def find_function(tree: ast.Module, name: str):
	"""Find the concrete implementation; ambiguous scopes and overload-only stubs fail."""
	node = find_definition(tree, name)
	if node is None and "." not in name:
		if name in scope_bindings(tree.body):
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
