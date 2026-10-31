"""Scope-aware Python source inspection without importing application code."""

import ast
from dataclasses import dataclass, replace

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


class SourceAnalyzer(EffectAnalyzer):
	"""Identify typing overloads using the decorator binding at definition time."""

	def __init__(self):
		super().__init__()
		self.overload_definitions = set()
		self.changed_overload_modules = set()

	def value(self, node, scope):
		value = super().value(node, scope)
		if isinstance(node, ast.Attribute) and node.attr == "overload":
			modules = self.value(node.value, scope).modules
			if modules & self.changed_overload_modules or any(
				self.module_writes.get(module, set()) & {"overload", None} for module in modules
			):
				value = replace(value, uncertain=True)
		return value

	def bind(self, target, value, scope, seen=frozenset(), *, anchor=True):
		if isinstance(target, ast.Attribute) and target.attr == "overload":
			self.changed_overload_modules.update(
				self.value(target.value, scope).modules & {"typing", "typing_extensions"}
			)
		return super().bind(target, value, scope, seen, anchor=anchor)

	def expression(self, node, scope, seen):
		if isinstance(node, ast.Call) and self.value(node.func, scope).modules & {
			"builtins.setattr",
			"builtins.delattr",
		}:
			attribute = node.args[1] if len(node.args) > 1 else None
			if node.args and (not isinstance(attribute, ast.Constant) or attribute.value == "overload"):
				self.changed_overload_modules.update(
					self.value(node.args[0], scope).modules & {"typing", "typing_extensions"}
				)
		return super().expression(node, scope, seen)

	def captured_decorators(self, node, values):
		if isinstance(node, FUNCTION_NODES):
			for value in values:
				if value.modules & {"typing.overload", "typing_extensions.overload"}:
					if value.uncertain:
						self.invalid_definitions.add(node)
					else:
						self.overload_definitions.add(node)

	def visit(self, node, scope, seen):
		self.changed_overload_modules.update(
			module
			for module, attributes in self.module_writes.items()
			if "overload" in attributes or None in attributes
		)
		result = super().visit(node, scope, seen)
		if (
			isinstance(node, ast.ImportFrom)
			and node.level == 0
			and node.module in self.changed_overload_modules
		):
			for alias in node.names:
				if alias.name == "overload":
					name = alias.asname or alias.name
					scope.set(name, replace(scope.get(name), uncertain=True), anchor=False)
		return result


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
		if any(effect.replaced_definition == name and effect.node is statement for effect in effects):
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
			if node in overloads:
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
	objects = {}
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
		exact_augmentation = False
		if (
			isinstance(statement, ast.AugAssign)
			and isinstance(statement.target, ast.Name)
			and isinstance(statement.op, ast.Add)
		):
			try:
				exact_augmentation = isinstance(literal(statement.target), (list, tuple))
				literal(statement.value)
			except ValueError, TypeError, KeyError, SyntaxError:
				exact_augmentation = False
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
			if effect.opaque and "__all__" in values:
				raise AmbiguousBinding(
					f"unknown call may modify __all__ at line {getattr(node, 'lineno', statement.lineno)}"
				)
			if effect.namespace:
				raise AmbiguousBinding(f"dynamic namespace at line {node.lineno}")
			for name in effect.bindings:
				values[name] = unknown
			for origin in () if exact_augmentation and effect.node is statement else effect.origins:
				if origin in objects:
					invalid.update(
						{id(objects[origin]): objects[origin]}
						if effect.shallow
						else mutable_objects(objects[origin])
					)
		if isinstance(statement, (ast.Assign, ast.AnnAssign)):
			targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
			names = [target.id for target in targets if isinstance(target, ast.Name)]
			try:
				value = literal(statement.value)
			except ValueError, TypeError, KeyError, SyntaxError:
				value = unknown
			values.update(dict.fromkeys(names, value))
			if value is not unknown:
				for name in names:
					for origin in analyzer.scope.get(name).origins:
						objects.setdefault(origin, value)
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
	analyzer = SourceAnalyzer()
	for statement in runtime_body(tree.body):
		analyzer.scan(statement)
	node = tree
	for part in parts:
		if not isinstance(node, (ast.Module, ast.ClassDef)):
			return None
		body = analyzer.runtime_bodies.get(node, node.body)
		writes = analyzer.member_writes.get(node, set())
		if part in writes or None in writes:
			return None
		try:
			binding = select_binding(body, part, overloads=analyzer.overload_definitions)
		except AmbiguousBinding:
			return None
		if binding is None or not isinstance(binding.node, DEFINITION_NODES):
			return None
		if binding.node in analyzer.invalid_definitions:
			return None
		node = binding.node
	return node


def find_function(tree: ast.Module, name: str):
	"""Find the concrete implementation; ambiguous scopes and overload-only stubs fail."""
	node = find_definition(tree, name)
	if node is None and "." not in name:
		analyzer = SourceAnalyzer()
		module_body = list(runtime_body(tree.body))
		for statement in module_body:
			analyzer.scan(statement)
		bindings = scope_bindings(module_body)
		if name in bindings or "*" in bindings:
			return None
		candidates = []
		visited = set()

		def body_for(scope):
			return module_body if scope is tree else analyzer.runtime_bodies.get(scope, scope.body)

		def relevant(scope):
			return any(
				(isinstance(item, FUNCTION_NODES) and item.name == name)
				or (isinstance(item, ast.ClassDef) and relevant(item))
				for item in scope_statements(body_for(scope))
			)

		def collect(scope, prefix=""):
			body = body_for(scope)
			if any(isinstance(item, FUNCTION_NODES) and item.name == name for item in scope_statements(body)):
				candidates.append(find_definition(tree, prefix + name))
			class_names = dict.fromkeys(
				item.name
				for item in scope_statements(body)
				if isinstance(item, ast.ClassDef) and relevant(item)
			)
			for class_name in class_names:
				try:
					binding = select_binding(body, class_name, overloads=analyzer.overload_definitions)
				except AmbiguousBinding:
					candidates.append(None)
					continue
				if binding is None or not isinstance(binding.node, ast.ClassDef):
					continue
				active = find_definition(tree, f"{prefix}{class_name}")
				if active is None:
					candidates.append(None)
				elif id(active) not in visited:
					visited.add(id(active))
					collect(active, f"{prefix}{class_name}.")

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
