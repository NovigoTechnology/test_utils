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


def find_definition(tree: ast.Module | ast.ClassDef, qualified_name: str):
	parts = qualified_name.split(".")
	node = tree
	decorators = set()
	for part in parts:
		if not isinstance(node, (ast.Module, ast.ClassDef)):
			return None
		decorators.update(overload_names(node.body))
		matches = [
			item
			for item in scope_statements(node.body)
			if isinstance(item, DEFINITION_NODES) and item.name == part
		]
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
