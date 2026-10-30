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
	if isinstance(node, ast.AnnAssign) and node.value is None:
		return
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
	typing_names = {}
	checking_names = set()
	changed_modules = set()
	functions = {}
	for statement in body:
		guard = isinstance(statement, ast.If) and (
			(isinstance(statement.test, ast.Name) and statement.test.id in checking_names)
			or (
				isinstance(statement.test, ast.Attribute)
				and isinstance(statement.test.value, ast.Name)
				and statement.test.value.id in typing_names
				and typing_names[statement.test.value.id] not in changed_modules
				and statement.test.attr == "TYPE_CHECKING"
			)
		)
		statements = statement.orelse if guard else [statement]
		for active in statements:
			bindings = set(scope_bindings([active]))
			for node, global_names in called_export_effects(active, functions):
				if global_names:
					bindings.update(scope_bindings([node]).keys() & global_names)
				if dynamic_namespace(node):
					checking_names.clear()
					changed_modules.update({"typing", "typing_extensions"})
				if (
					isinstance(node, ast.Attribute)
					and isinstance(node.ctx, (ast.Store, ast.Del))
					and node.attr == "TYPE_CHECKING"
					and isinstance(node.value, ast.Name)
					and node.value.id in typing_names
				):
					changed_modules.add(typing_names[node.value.id])
			alias_module = (
				typing_names.get(active.value.id)
				if (isinstance(active, ast.Assign) and isinstance(active.value, ast.Name))
				else None
			)
			if "*" in bindings:
				typing_names.clear()
				checking_names.clear()
			for name in bindings:
				typing_names.pop(name, None)
			checking_names.difference_update(bindings)
			if alias_module:
				for target in active.targets:
					if isinstance(target, ast.Name):
						typing_names[target.id] = alias_module
			if isinstance(active, ast.Import):
				for alias in active.names:
					name = alias.asname or alias.name.split(".")[0]
					typing_names.pop(name, None)
					checking_names.discard(name)
					if alias.name in {"typing", "typing_extensions"}:
						typing_names[name] = alias.name
			elif (
				isinstance(active, ast.ImportFrom)
				and active.level == 0
				and active.module in {"typing", "typing_extensions"}
			):
				for alias in active.names:
					name = alias.asname or alias.name
					typing_names.pop(name, None)
					checking_names.discard(name)
					if alias.name == "TYPE_CHECKING" and active.module not in changed_modules:
						checking_names.add(name)
			update_local_functions(active, functions)
			yield active


def ordered_bindings(body, name):
	"""Keep declaration order and the statement responsible for each binding."""
	functions = {}
	for statement in runtime_body(body):
		for node, global_names in called_export_effects(statement, functions):
			if dynamic_namespace(node) or (
				global_names and name in global_names and name in scope_bindings([node])
			):
				# Definition-time effects happen before the definition binds its own name.
				target = ast.copy_location(ast.Name(id=name, ctx=ast.Store()), statement)
				yield Binding(target, statement, conditional=True)
				break
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
		update_local_functions(statement, functions)


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


def update_local_functions(statement, functions):
	"""Track local helper definitions and simple aliases in declaration order."""
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


def dynamic_namespace(node):
	return (
		isinstance(node, ast.Call)
		and isinstance(node.func, ast.Name)
		and node.func.id in {"globals", "locals", "vars", "exec", "eval"}
		# vars(obj) inspects that object's attributes, rather than the active namespace.
		and (
			node.func.id != "vars" or not node.args or any(isinstance(arg, ast.Starred) for arg in node.args)
		)
	)


def called_export_effects(node, functions, seen=frozenset(), *, global_names=None):
	"""Walk executed syntax and known helpers, retaining their explicit global bindings."""
	for child in executable_nodes(node):
		yield child, global_names
		calls = []
		if isinstance(child, ast.Call):
			calls.append(child.func)
		if isinstance(child, DEFINITION_NODES):
			# Applying a bare decorator invokes it without an explicit Call in the AST.
			calls.extend(item for item in child.decorator_list if isinstance(item, ast.Name))
		for callee in calls:
			function = functions.get(callee.id) if isinstance(callee, ast.Name) else None
			if function is None or id(function) in seen:
				continue
			helper_globals = frozenset(
				name
				for statement in scope_statements(function.body)
				if isinstance(statement, ast.Global)
				for name in statement.names
			)
			local_functions = functions.copy()
			for statement in function.body:
				yield from called_export_effects(
					statement, local_functions, seen | {id(function)}, global_names=helper_globals
				)
				update_local_functions(statement, local_functions)


def mutable_objects(value, seen=None):
	"""Keep identity across aliases, including containers nested inside other values."""
	seen = set() if seen is None else seen
	if not isinstance(value, (dict, list, tuple, set)) or id(value) in seen:
		return {}
	seen.add(id(value))
	result = {id(value): value}
	children = [*value.keys(), *value.values()] if isinstance(value, dict) else value
	for child in children:
		result.update(mutable_objects(child, seen))
	return result


def static_exports(body, *, wildcard=None):
	"""Return a proven __all__, or None for ordinary public-name exports."""
	unknown = object()
	values = {}
	invalid = {}
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
			if value is unknown or mutable_objects(value).keys() & invalid.keys():
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
		if isinstance(statement, ast.AnnAssign) and statement.value is None:
			continue
		for node in scope_statements([statement]):
			if not isinstance(node, ast.ImportFrom) or not any(alias.name == "*" for alias in node.names):
				continue
			# Conditional imports can also replace constant aliases or __all__ itself.
			for name in values.keys() - {"__all__"}:
				values[name] = unknown
			if node.lineno >= last_reset and (wildcard is None or wildcard(node)):
				values["__all__"] = unknown
		for node, global_names in called_export_effects(statement, functions):
			if dynamic_namespace(node):
				raise AmbiguousBinding(f"dynamic namespace at line {node.lineno}")
			if global_names:
				for name in scope_bindings([node]).keys() & global_names:
					values[name] = unknown
			if isinstance(node, ast.Call) or (
				isinstance(node, (ast.Subscript, ast.Attribute))
				and isinstance(node.ctx, (ast.Store, ast.Del))
			):
				for child in ast.walk(node):
					if isinstance(child, ast.Name) and child.id in values:
						invalid.update(mutable_objects(values[child.id]))
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
		update_local_functions(statement, functions)
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
