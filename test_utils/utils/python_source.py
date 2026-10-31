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


def runtime_body(tree: ast.Module | ast.ClassDef, owner=None) -> list[ast.stmt]:
	"""Prune proven typing-only branches; mutation makes further guard proofs unsupported."""
	bodies = {}
	module_guards = set()
	unsupported = False

	def globals_in(statements):
		return {
			name
			for node in scope_statements(statements)
			if isinstance(node, ast.Global)
			for name in node.names
		}

	def forget(guards, names):
		guards.difference_update(
			guard for guard in list(guards) if "*" in names or guard.split(".", 1)[0] in names
		)

	def collect(statements, guards, global_names=frozenset()):
		nonlocal unsupported
		result = []
		for statement in statements:
			if unsupported:
				guards.clear()
			if isinstance(statement, ast.If) and decorator_name(statement.test) in guards:
				result.extend(collect(statement.orelse, guards, global_names))
				continue
			result.append(statement)
			bindings = scope_bindings([statement])
			expressions = (
				definition_expressions(statement) if isinstance(statement, ast.ClassDef) else [statement]
			)
			nodes = [node for expression in expressions for node in executable_nodes(expression)]
			possible_globals = global_names & bindings.keys()
			# Compound statements remain opaque; collect only their possible global writes.
			for node in nodes:
				if isinstance(node, ast.ClassDef):
					possible_globals |= globals_in(node.body) & scope_bindings(node.body).keys()
			if any(
				isinstance(node, ast.Attribute)
				and isinstance(node.ctx, (ast.Store, ast.Del))
				and node.attr == "TYPE_CHECKING"
				for node in nodes
			) or any(guard.split(".", 1)[0] in possible_globals for guard in module_guards | guards):
				unsupported = True
				module_guards.clear()
				guards.clear()
			# A class name is bound after its body; expressions may bind names first.
			forget(
				guards,
				{name for name, nodes in bindings.items() if any(node is not statement for node in nodes)}
				if isinstance(statement, ast.ClassDef)
				else bindings,
			)
			if isinstance(statement, ast.ClassDef):
				# Enclosing class locals are not globals of a nested class body.
				bodies[statement] = collect(statement.body, module_guards.copy(), globals_in(statement.body))
				forget(guards, {statement.name})
			elif isinstance(statement, (ast.Import, ast.ImportFrom)) and not unsupported:
				for alias in statement.names:
					local = alias.asname or (
						alias.name.split(".")[0] if isinstance(statement, ast.Import) else alias.name
					)
					forget(guards, {local})
					if isinstance(statement, ast.Import) and alias.name in {"typing", "typing_extensions"}:
						guards.add(f"{local}.TYPE_CHECKING")
					elif (
						isinstance(statement, ast.ImportFrom)
						and not statement.level
						and statement.module in {"typing", "typing_extensions"}
						and alias.name == "TYPE_CHECKING"
					):
						guards.add(local)
		return result

	bodies[tree] = collect(
		tree.body, module_guards if isinstance(tree, ast.Module) else set(), globals_in(tree.body)
	)
	return bodies.get(owner or tree, (owner or tree).body)


def runtime_statements(body: list[ast.stmt]) -> list[ast.stmt]:
	"""Filter a standalone body when no enclosing module context is available."""
	return runtime_body(ast.Module(body=body, type_ignores=[]))


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
		body = runtime_body(tree, node)
		decorators.update(overload_names(body))
		try:
			binding = select_binding(body, part, decorators=decorators)
		except AmbiguousBinding:
			return None
		if binding is None or not isinstance(binding.node, DEFINITION_NODES):
			return None
		node = binding.node
	return node


def unique_definition(tree: ast.Module | ast.ClassDef, qualified_name: str):
	"""Identify one explicit declaration for metadata, leaving wildcard checks to the resolver.

	A dotted tracking name cannot distinguish two declarations of the same owner.
	Explicit rebinding still invalidates it; wildcard membership requires source files.
	"""
	node = tree
	decorators = set()
	for part in qualified_name.split("."):
		if not isinstance(node, (ast.Module, ast.ClassDef)):
			return None
		body = runtime_body(tree, node)
		decorators.update(overload_names(body))
		declarations = [
			item
			for item in scope_bindings(body).get(part, [])
			if isinstance(item, DEFINITION_NODES)
			and not (
				isinstance(item, FUNCTION_NODES)
				and any(decorator_name(dec) in decorators for dec in item.decorator_list)
			)
		]
		if len(declarations) != 1:
			return None
		try:
			binding = select_binding(body, part, decorators=decorators, wildcard=lambda _node, _name: False)
		except AmbiguousBinding:
			return None
		if binding is None or binding.node is not declarations[0]:
			return None
		node = binding.node
	return node


def find_function(tree: ast.Module, name: str):
	"""Find the concrete implementation; ambiguous scopes and overload-only stubs fail."""

	def tracked_definition(qualified_name):
		selected = find_definition(tree, qualified_name)
		return selected if selected is unique_definition(tree, qualified_name) else None

	node = tracked_definition(name)
	if node is None and "." not in name:
		bindings = scope_bindings(runtime_body(tree))
		if name in bindings or "*" in bindings:
			return None
		candidates = []

		def collect(scope, prefix=""):
			body = runtime_body(tree, scope)
			if any(isinstance(item, FUNCTION_NODES) and item.name == name for item in scope_statements(body)):
				candidates.append(tracked_definition(prefix + name))
			for item in scope_statements(body):
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
