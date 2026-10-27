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


def find_definition(tree: ast.Module | ast.ClassDef, qualified_name: str):
	parts = qualified_name.split(".")
	node = tree
	for part in parts:
		if not isinstance(node, (ast.Module, ast.ClassDef)):
			return None
		node = next(
			(
				item
				for item in scope_statements(node.body)
				if isinstance(item, DEFINITION_NODES) and item.name == part
			),
			None,
		)
		if node is None:
			return None
	return node


def extract_function(source: str, name: str) -> str | None:
	"""Resolve a qualified method, or a unique legacy unqualified method name."""
	try:
		tree = ast.parse(source)
	except SyntaxError:
		return None
	node = find_definition(tree, name)
	if node is None and "." not in name:
		candidates = []

		def collect(scope):
			for item in scope_statements(scope.body):
				if isinstance(item, FUNCTION_NODES) and item.name == name:
					candidates.append(item)
				elif isinstance(item, ast.ClassDef):
					collect(item)

		collect(tree)
		if len(candidates) == 1:
			node = candidates[0]
	if not isinstance(node, FUNCTION_NODES):
		return None
	start = min([node.lineno, *(dec.lineno for dec in node.decorator_list)])
	return "\n".join(source.splitlines()[start - 1 : node.end_lineno])
