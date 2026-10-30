"""Scope-aware Python source inspection without importing application code."""

import ast
from dataclasses import dataclass

from test_utils.utils.python_ast import (
	DEFINITION_NODES as DEFINITION_NODES,
)
from test_utils.utils.python_ast import (
	FUNCTION_NODES as FUNCTION_NODES,
)
from test_utils.utils.python_ast import (
	definition_expressions as definition_expressions,
)
from test_utils.utils.python_ast import (
	scope_bindings as scope_bindings,
)
from test_utils.utils.python_ast import (
	scope_statements as scope_statements,
)
from test_utils.utils.python_scope import EffectAnalyzer


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
	analyzer = EffectAnalyzer()
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
			for effect in analyzer.scan(active):
				bindings.update(effect.bindings)
				if effect.namespace:
					checking_names.clear()
					changed_modules.update({"typing", "typing_extensions"})
				changed_modules.update(effect.typing_modules)
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
			yield active


def ordered_bindings(body, name):
	"""Keep declaration order and the statement responsible for each binding."""
	analyzer = EffectAnalyzer()
	for statement in runtime_body(body):
		effects = analyzer.scan(statement)
		for effect in effects:
			if effect.namespace or name in effect.bindings:
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
		if any(effect.replaced_definition == name for effect in effects):
			yield Binding(
				ast.copy_location(ast.Name(id=name, ctx=ast.Store()), statement), statement, conditional=True
			)


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
	analyzer = EffectAnalyzer()
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
		for effect in analyzer.scan(statement):
			node = effect.node
			if effect.namespace:
				raise AmbiguousBinding(f"dynamic namespace at line {node.lineno}")
			for name in effect.bindings:
				values[name] = unknown
			if (
				effect.mutation
				or isinstance(node, ast.Call)
				or (
					isinstance(node, (ast.Subscript, ast.Attribute))
					and isinstance(node.ctx, (ast.Store, ast.Del))
				)
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
