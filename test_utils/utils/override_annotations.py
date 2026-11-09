"""Discover Frappe overrides and validate upstream annotations using local ASTs."""

import ast
import json
import re
import subprocess
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath

from test_utils.utils.namespace_types import class_values_are_safe
from test_utils.utils.override_metadata import (
	annotation_field_errors,
	github_repository,
	parse_annotation,
	repository_identity,
)
from test_utils.utils.python_source import (
	DEFINITION_NODES,
	FUNCTION_NODES,
	AmbiguousBinding,
	definition_expressions,
	executable_nodes,
	find_function,
	overload_names,
	runtime_body,
	scope_bindings,
	scope_statements,
	select_binding,
	static_attribute,
	unique_definition,
)

HOOKS = {"required_apps", "override_whitelisted_methods", "override_doctype_class", "extend_doctype_class"}


class ResolutionError(ValueError):
	"""An override cannot be verified from the configured checkouts."""


class MissingSymbolError(ResolutionError):
	"""A readable namespace proves that the requested declaration is absent."""

	def __init__(self, message, *, owner_resolved=False):
		super().__init__(message)
		self.owner_resolved = owner_resolved


class OwnerResolutionError(ResolutionError):
	"""A module-assignment owner is imported from a dependency but cannot be verified."""


def read_tree(path: Path) -> ast.Module:
	try:
		return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
	except (OSError, UnicodeError, SyntaxError) as exc:
		raise ResolutionError(f"Cannot parse {path}: {exc}") from exc


def dotted_name(node: ast.expr) -> str:
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		return f"{dotted_name(node.value)}.{node.attr}"
	raise ResolutionError(f"Unsupported dynamic reference at line {node.lineno}")


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


def literal(node: ast.expr, values: dict, invalid=None):
	"""Evaluate only data expressions, never calls to application code."""
	if isinstance(node, ast.Name) and node.id in values:
		if invalid and mutable_objects(values[node.id]).keys() & invalid.keys():
			raise ResolutionError(f"Container {node.id!r} was mutated dynamically at line {node.lineno}")
		return values[node.id]
	if isinstance(node, ast.Constant):
		return node.value
	if isinstance(node, ast.Subscript):
		try:
			return literal(node.value, values, invalid)[literal(node.slice, values, invalid)]
		except KeyError, IndexError, TypeError:
			raise ResolutionError(f"Unsupported container lookup at line {node.lineno}") from None
	if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
		collection = {ast.List: list, ast.Tuple: tuple, ast.Set: set}[type(node)]
		return collection(literal(item, values, invalid) for item in node.elts)
	if isinstance(node, ast.Dict):
		result = {}
		for key, value in zip(node.keys, node.values, strict=True):
			if key is None:
				result.update(literal(value, values, invalid))
			else:
				result[literal(key, values, invalid)] = literal(value, values, invalid)
		return result
	if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
		left, right = literal(node.left, values, invalid), literal(node.right, values, invalid)
		return left | right if isinstance(node.op, ast.BitOr) else left + right
	raise ResolutionError(f"Unsupported dynamic hook expression at line {node.lineno}; use static values")


def mutation_references(node, functions=None, seen=frozenset(), controlled=False):
	"""Unsupported operations that can mutate a known container or one of its aliases."""
	if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
		if controlled and not isinstance(node, ast.Lambda):
			yield ast.copy_location(ast.Name(id=node.name, ctx=ast.Store()), node)
		for expression in definition_expressions(node):
			yield from mutation_references(expression, functions, seen, controlled)
		for decorator in getattr(node, "decorator_list", []):
			# A bare decorator is called implicitly after evaluating the definition.
			call = ast.copy_location(ast.Call(func=decorator, args=[], keywords=[]), decorator)
			yield from mutation_references(call, functions, seen, controlled)
		if isinstance(node, ast.ClassDef):
			for statement in node.body:
				for child in executable_nodes(statement):
					if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
						# Class-local aliases of known containers are unsupported: taint
						# their source rather than silently ignoring later mutations.
						yield child
					elif isinstance(child, ast.Call):
						yield from mutation_references(child, functions, seen)
					elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
						yield from mutation_references(child, functions, seen)
					elif isinstance(child, ast.Global):
						for name in child.names:
							yield ast.copy_location(ast.Name(id=name, ctx=ast.Store()), child)
		return
	if controlled:
		binding = None
		if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
			binding = node.name
		elif isinstance(node, ast.MatchMapping):
			binding = node.rest
		if binding:
			yield ast.copy_location(ast.Name(id=binding, ctx=ast.Store()), node)
	if isinstance(node, (ast.Import, ast.ImportFrom)):
		yield node
	elif isinstance(node, ast.NamedExpr):
		yield node.target
	elif isinstance(node, ast.Name) and controlled and isinstance(node.ctx, (ast.Store, ast.Del)):
		yield node
	elif isinstance(node, ast.Call):
		yield node
		if functions and isinstance(node.func, ast.Name) and node.func.id in functions:
			function = functions[node.func.id]
			if id(function) not in seen:
				body = function.body if isinstance(function.body, list) else [function.body]
				for statement in body:
					yield from mutation_references(
						statement, functions, seen | {id(function)}, controlled=True
					)
	elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
		targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
		for target in targets:
			if controlled or not isinstance(target, ast.Name):
				yield target
	for child in ast.iter_child_nodes(node):
		yield from mutation_references(
			child,
			functions,
			seen,
			controlled
			or isinstance(node, (ast.If, ast.For, ast.While, ast.Try, ast.TryStar, ast.With, ast.Match)),
		)


def read_hooks(path: Path, *, only: set[str] | None = None) -> dict:
	selected = HOOKS if only is None else only
	values = {}
	invalid = {}
	functions = {}
	for node in read_tree(path).body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
			if node.name in selected:
				raise ResolutionError(f"{path}:{node.lineno}: hooks require static assignments")
			values.pop(node.name, None)
			functions.pop(node.name, None)
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			functions[node.name] = node
		for reference in mutation_references(node, functions):
			if isinstance(reference, (ast.Import, ast.ImportFrom)):
				for alias in reference.names:
					name = alias.asname or (
						alias.name.split(".")[0] if isinstance(reference, ast.Import) else alias.name
					)
					if name in selected or alias.name == "*":
						raise ResolutionError(
							f"{path}:{reference.lineno}: hook-binding imports are unsupported; use static assignments"
						)
					values.pop(name, None)
					functions.pop(name, None)
			for child in ast.walk(reference):
				if isinstance(child, ast.Name):
					if isinstance(child.ctx, (ast.Store, ast.Del)) and child.id in selected:
						raise ResolutionError(
							f"{path}:{child.lineno}: dynamic configuration of {child.id}; use static assignments"
						)
					if child.id in values:
						invalid.update(mutable_objects(values[child.id]))
					if isinstance(child.ctx, (ast.Store, ast.Del)):
						values.pop(child.id, None)
						functions.pop(child.id, None)
		if isinstance(node, (ast.Assign, ast.AnnAssign)):
			targets = node.targets if isinstance(node, ast.Assign) else [node.target]
			names = [target.id for target in targets if isinstance(target, ast.Name)]
			if isinstance(node.value, ast.Name) and node.value.id in functions:
				for name in names:
					functions[name] = functions[node.value.id]
			indirect_names = {
				child.id for target in targets for child in ast.walk(target) if isinstance(child, ast.Name)
			}
			if any(not isinstance(target, (ast.Name, ast.Subscript)) for target in targets):
				if indirect_names & selected:
					raise ResolutionError(f"{path}:{node.lineno}: hooks require simple static assignments")
				for name in indirect_names:
					values.pop(name, None)
					functions.pop(name, None)
				if node.value is not None:
					for child in ast.walk(node.value):
						if isinstance(child, ast.Name) and child.id in values:
							invalid.update(mutable_objects(values[child.id]))
			if any(
				isinstance(target, ast.Subscript)
				and any(isinstance(child, ast.Name) and child.id in selected for child in ast.walk(target))
				for target in targets
			):
				raise ResolutionError(
					f"{path}:{node.lineno}: hook item assignment is unsupported; use a static dictionary"
				)
			try:
				value = literal(node.value, values, invalid)
			except ResolutionError, TypeError, ValueError, AttributeError:
				if selected.intersection(names):
					raise ResolutionError(f"{path}:{node.lineno}: hook {names} must use static values")
				for name in names:
					values.pop(name, None)
				if node.value is not None:
					for child in ast.walk(node.value):
						if isinstance(child, ast.Name) and child.id in values:
							invalid.update(mutable_objects(values[child.id]))
				continue
			values.update(dict.fromkeys(names, value))
		elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
			name = node.target.id
			try:
				value = literal(
					ast.copy_location(ast.BinOp(left=node.target, op=node.op, right=node.value), node),
					values,
					invalid,
				)
				# += on lists and |= on dictionaries also update aliases in Python.
				if isinstance(values.get(name), list) and isinstance(value, list):
					values[name][:] = value
				elif isinstance(values.get(name), dict) and isinstance(value, dict):
					values[name].update(value)
				else:
					values[name] = value
			except (ResolutionError, TypeError, KeyError) as exc:
				if name in values:
					invalid.update(mutable_objects(values[name]))
				values.pop(name, None)
				if name in selected:
					raise ResolutionError(f"{path}:{node.lineno}: cannot statically evaluate {name}") from exc
		else:
			if isinstance(node, ast.Delete):
				for target in node.targets:
					if isinstance(target, ast.Name):
						values.pop(target.id, None)
						functions.pop(target.id, None)
			# Conditional assignments and mutations must not silently hide registrations.
			for child in (
				[]
				if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
				else ast.walk(node)
			):
				if isinstance(child, ast.Name) and child.id in selected:
					raise ResolutionError(
						f"{path}:{child.lineno}: dynamic configuration of {child.id}; use top-level static assignments"
					)
	for name in selected:
		if name in values and mutable_objects(values[name]).keys() & invalid.keys():
			raise ResolutionError(
				f"{path}: hook {name!r} was mutated dynamically through an alias; use static values"
			)
	return {name: values[name] for name in selected if name in values}


def normalize_required_app(value: str) -> tuple[str, str | None]:
	if not isinstance(value, str) or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)?", value):
		raise ResolutionError(f"Invalid required_apps entry: {value!r}; expected app or owner/repository")
	name = value.rsplit("/", 1)[-1].removesuffix(".git").replace("-", "_")
	if not name.isidentifier():
		raise ResolutionError(f"Invalid app name in required_apps: {value!r}")
	return name, f"https://github.com/{value.removesuffix('.git')}" if "/" in value else None


@dataclass
class App:
	name: str
	root: Path
	package: Path
	fallback_repo: str | None = None

	@cached_property
	def repository(self) -> str | None:
		try:
			result = subprocess.run(
				["git", "-C", str(self.root), "config", "--get", "remote.origin.url"],
				capture_output=True,
				text=True,
				check=False,
			)
		except OSError:
			return self.fallback_repo
		return result.stdout.strip() or self.fallback_repo


def locate_app(path: Path, name: str | None = None, fallback_repo: str | None = None) -> App:
	path = path.resolve()
	if not path.is_dir():
		raise ResolutionError(f"App checkout not found: {path}; check out required apps under --apps-root")
	if (path / "hooks.py").is_file():
		return App(name or path.name, path.parent, path, fallback_repo)
	if name and (path / name).is_dir():
		return App(name, path, path / name, fallback_repo)
	candidates = [p.parent for p in sorted(path.glob("*/hooks.py"))]
	if len(candidates) == 1:
		return App(candidates[0].name, path, candidates[0], fallback_repo)
	if name is None and (path / path.name).is_dir():
		return App(path.name, path, path / path.name, fallback_repo)
	raise ResolutionError(f"Cannot identify app package in {path}")


@dataclass
class Module:
	app: App
	name: str
	path: Path
	tree: ast.Module

	def import_reference(self, statement: ast.stmt, alias: ast.alias | None = None) -> str:
		if isinstance(statement, ast.Import):
			return alias.name if alias.asname else alias.name.split(".")[0]
		prefix = statement.module or ""
		if statement.level:
			package = self.name.split(".") if self.path.name == "__init__.py" else self.name.split(".")[:-1]
			if statement.level > len(package):
				raise ResolutionError(f"Relative import escapes package at {self.path}:{statement.lineno}")
			prefix = ".".join(package[: len(package) - statement.level + 1] + ([prefix] if prefix else []))
		return prefix if alias is None or alias.name == "*" else f"{prefix}.{alias.name}"

	@cached_property
	def imports(self) -> dict[str, str]:
		result = {}
		for node in scope_statements(self.tree.body):
			if isinstance(node, (ast.Import, ast.ImportFrom)):
				for alias in node.names:
					if alias.name != "*":
						local = alias.asname or (
							alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
						)
						result[local] = self.import_reference(node, alias)
		return result


@dataclass(frozen=True)
class ModuleResolution:
	state: str
	references: tuple[str, ...]
	module: Module | None = None
	error: ResolutionError | None = None

	@property
	def reference(self):
		return self.references[0] if len(self.references) == 1 else " | ".join(self.references)


type DeclarationId = tuple[Path, int, int]


@dataclass
class Symbol:
	module: Module
	qualname: str
	node: ast.AST
	# Active import prefixes let a circular reexport see declarations already made.
	import_limits: tuple[tuple[Path, tuple[int, int]], ...] = field(default=(), repr=False)

	@property
	def reference(self) -> str:
		return f"{self.module.name}.{self.qualname}"

	@property
	def identity(self) -> DeclarationId:
		"""Dotted names can be reused; source declarations remain distinct."""
		return self.module.path, self.node.lineno, self.node.col_offset


@dataclass(frozen=True)
class FunctionResolution:
	state: str
	symbol: Symbol | None = None
	error: ResolutionError | None = None


class SourceResolver:
	def __init__(self, apps: list[App], *, require_unique_definitions: bool = False):
		self.apps = {app.name: app for app in apps}
		self.modules: dict[Path, Module] = {}
		self._verified_class_namespaces = set()
		self._checking_class_namespaces = set()
		# Tracking metadata requires uniqueness; MRO resolution also uses declaration-time prefixes.
		self.require_unique_definitions = require_unique_definitions

	# Revision-backed callers override source access, retaining the same analysis.
	read_tree = staticmethod(read_tree)
	is_file = staticmethod(Path.is_file)
	is_dir = staticmethod(Path.is_dir)

	def module(self, app: App, name: str, path: Path) -> Module:
		if path not in self.modules:
			self.modules[path] = Module(app, name, path, self.read_tree(path))
		return self.modules[path]

	def source_module(self, reference: str, *, require_symbol: bool = False) -> tuple[Module, list[str]]:
		parts = reference.split(".")
		if not all(part.isidentifier() for part in parts):
			raise ResolutionError(f"Invalid dotted Python reference: {reference!r}")
		app = self.apps.get(parts[0])
		if app is None:
			raise ResolutionError(
				f"Cannot resolve {reference}: app {parts[0]!r} is not in required_apps (or frappe)"
			)
		# Symbols follow package bindings first; imports name an exact provider.
		ends = range(1, len(parts)) if require_symbol else [len(parts)]
		for end in ends:
			stem = app.package.joinpath(*parts[1:end])
			for path in (stem.with_suffix(".py"), stem / "__init__.py"):
				if not self.is_file(path):
					continue
				return self.module(app, ".".join(parts[:end]), path), parts[end:]
			if self.is_dir(stem):
				# A namespace package has no declarations of its own.
				return Module(
					app, ".".join(parts[:end]), stem / "__init__.py", ast.Module(body=[], type_ignores=[])
				), parts[end:]
		raise ResolutionError(
			f"Cannot resolve {reference}: source file not found; check --apps-root and upstream renames"
		)

	@staticmethod
	def module_body(module, limits):
		limit = min((position for path, position in limits if path == module.path), default=(float("inf"), 0))
		return [node for node in runtime_body(module.tree) if (node.lineno, node.col_offset) < limit]

	def exports(self, module, name, limits, seen):
		"""Query one export; never execute a module or enumerate its dependencies."""
		body = self.module_body(module, limits)
		key = (module.path, name, len(body), "exports")
		if key in seen:
			raise ResolutionError(f"Ambiguous export cycle for {module.name}.{name}")
		seen = seen | {key}
		bindings = scope_bindings(body)
		if self.dynamic_namespace([body], {name, "__all__"}, module.tree, module=module):
			raise ResolutionError(f"Ambiguous dynamic namespace in {module.path}")
		all_bindings = bindings.get("__all__", [])
		if all_bindings:
			assignment = all_bindings[0]
			try:
				if len(all_bindings) != 1 or assignment not in body:
					raise ValueError
				# Support only a direct, unaliased declaration of the export list.
				if isinstance(assignment, ast.Assign) and len(assignment.targets) == 1:
					target = assignment.targets[0]
				elif isinstance(assignment, ast.AnnAssign):
					target = assignment.target
				else:
					raise ValueError
				if not isinstance(target, ast.Name) or target.id != "__all__":
					raise ValueError
				if (
					self.binding(module, body[body.index(assignment) + 1 :], "__all__", limits, seen)
					is not None
				):
					raise ValueError
				values = ast.literal_eval(assignment.value)
				if not isinstance(values, (list, tuple)) or not all(
					isinstance(value, str) for value in values
				):
					raise ValueError
				if any(
					isinstance(node, ast.Name) and node.id == "__all__"
					for statement in body
					if statement is not assignment
					for node in executable_nodes(statement, tree=module.tree)
				):
					raise ValueError
			except (AttributeError, ValueError, TypeError, SyntaxError) as exc:
				raise ResolutionError(f"Ambiguous dynamic __all__ in {module.path}") from exc
			return name in values
		if "__getattr__" in bindings or self.dynamic_namespace(
			[body], {"__getattr__"}, module.tree, module=module
		):
			raise ResolutionError(f"Ambiguous dynamic exports (__getattr__) in {module.path}")
		if self.binding(module, body, "__all__", limits, seen) is not None:
			raise ResolutionError(f"Ambiguous reexported __all__ in {module.path}")
		if name.startswith("_"):
			return False
		binding = self.binding(module, body, name, limits, seen)
		if binding is None and any(path == module.path for path, _ in limits):
			raise ResolutionError(f"Ambiguous export cycle: {name!r} is not yet declared in {module.path}")
		return binding is not None

	def binding(self, module, body, name, limits, seen, decorators=None):
		def wildcard(statement, target):
			reference = module.import_reference(statement, statement.names[0])
			try:
				imported, remaining = self.source_module(reference)
				if remaining:
					raise ResolutionError(f"Source module {reference} not found")
				return self.exports(
					imported, target, (*limits, (module.path, (statement.lineno, statement.col_offset))), seen
				)
			except ResolutionError as exc:
				raise ResolutionError(
					f"Ambiguous wildcard import at {module.path}:{statement.lineno} ({reference}): {exc}"
				) from exc

		try:
			return select_binding(body, name, decorators=decorators, wildcard=wildcard)
		except AmbiguousBinding as exc:
			raise ResolutionError(
				f"Ambiguous resolution at {module.path}:{exc.statement.lineno}: {exc}"
			) from exc

	def resolve(self, reference: str) -> Symbol:
		module, parts = self.source_module(reference, require_symbol=True)
		return self.resolve_in(module, parts, (), frozenset(), dotted_path=True)

	def module_resolution(self, state, references, *, module=None, error=None):
		if isinstance(references, str):
			references = (references,)
		return ModuleResolution(
			state,
			tuple(sorted(dict.fromkeys(references))),
			module,
			error,
		)

	@staticmethod
	def direct_value_binding(body, name):
		bindings = scope_bindings(body).get(name, [])
		return (
			bindings[0]
			if len(bindings) == 1
			and bindings[0] in body
			and isinstance(bindings[0], (ast.Assign, ast.AnnAssign))
			else None
		)

	def resolve_module_in(self, module, parts, limits, seen, *, implicit_child=False):
		"""Classify an imported owner while preserving canonical dependency provenance."""
		body = self.module_body(module, limits)
		key = (module.path, ".".join(parts), len(body), "implicit_module" if implicit_child else "module")
		reference = ".".join([module.name, *parts])
		if key in seen:
			error = ResolutionError(f"Ambiguous cyclic module import for {reference}")
			return self.module_resolution("unresolved", reference, error=error)
		seen = seen | {key}
		if self.dynamic_namespace([body], parts, module.tree, module=module):
			error = ResolutionError(f"Ambiguous dynamic namespace in {module.path}")
			candidates = imported_owner_references(module, body, parts)
			return self.module_resolution("unresolved", candidates or [reference], error=error)
		try:
			binding = self.binding(module, body, parts[0], limits, seen)
		except ResolutionError as error:
			if value := self.direct_value_binding(body, parts[0]):
				return self.module_resolution(
					"non_module",
					f"{module.name}.{parts[0]}",
					error=ResolutionError(
						f"{module.name}.{parts[0]} is bound to a value at {module.path}:{value.lineno}"
					),
				)
			candidates = imported_owner_references(module, body, parts)
			return self.module_resolution("unresolved", candidates or [reference], error=error)
		if binding is None:
			try:
				child, remaining = self.source_module(reference)
			except ResolutionError as error:
				state = "non_module" if implicit_child else "unresolved"
				return self.module_resolution(state, reference, error=error)
			if remaining:
				error = ResolutionError(f"Source module {reference} not found")
				state = "non_module" if implicit_child else "unresolved"
				return self.module_resolution(state, reference, error=error)
			return self.module_resolution("module", child.name, module=child)
		if not isinstance(binding.node, ast.alias):
			error = ResolutionError(
				f"{module.name}.{parts[0]} is bound to a value at {module.path}:{binding.statement.lineno}"
			)
			return self.module_resolution("non_module", f"{module.name}.{parts[0]}", error=error)
		import_limits = (*limits, (module.path, (binding.statement.lineno, binding.statement.col_offset)))
		if isinstance(binding.statement, ast.ImportFrom):
			provider_reference = module.import_reference(binding.statement)
			target_reference = ".".join(
				[module.import_reference(binding.statement, binding.node), *parts[1:]]
			)
			try:
				provider, remaining = self.source_module(provider_reference)
			except ResolutionError as error:
				return self.module_resolution("unresolved", target_reference, error=error)
			if remaining:
				error = ResolutionError(f"Source module {provider_reference} not found")
				return self.module_resolution("unresolved", target_reference, error=error)
			return self.resolve_module_in(
				provider,
				[binding.node.name, *parts[1:]],
				import_limits,
				seen,
			)
		if not isinstance(binding.statement, ast.Import):
			error = ResolutionError(f"{module.name}.{parts[0]} is not an imported module")
			return self.module_resolution("non_module", reference, error=error)
		candidates = imported_owner_references(module, body, parts)
		if not binding.node.asname and len(candidates) == 1:
			base_reference = candidates[0]
			tail = []
		else:
			base_reference = None
		prefix = binding.node.name.split(".")
		if binding.node.asname:
			base_reference = binding.node.name
			tail = parts[1:]
		elif base_reference is None and parts[: len(prefix)] == prefix:
			base_reference = binding.node.name
			tail = parts[len(prefix) :]
		elif base_reference is None:
			base_reference = prefix[0]
			tail = parts[1:]
		try:
			owner, remaining = self.source_module(base_reference)
		except ResolutionError as error:
			return self.module_resolution("unresolved", base_reference, error=error)
		if remaining:
			error = ResolutionError(f"Source module {base_reference} not found")
			return self.module_resolution("unresolved", base_reference, error=error)
		if not tail:
			return self.module_resolution("module", owner.name, module=owner)
		return self.resolve_module_in(owner, tail, import_limits, seen, implicit_child=True)

	def resolve_function_in(self, module, parts, limits, seen):
		"""Resolve aliases to their terminal function or proven data binding."""
		try:
			symbol = self.resolve_in(module, parts, limits, seen)
		except ResolutionError as error:
			body = self.module_body(module, limits)
			key = (module.path, ".".join(parts), len(body), "function")
			if (
				key in seen
				or len(parts) != 1
				or self.dynamic_namespace([body], parts, module.tree, module=module)
			):
				return FunctionResolution("unresolved", error=error)
			seen = seen | {key}
			assignment = self.direct_value_binding(body, parts[0])
			if assignment is not None:
				value = assignment.value
				if value is None:
					return FunctionResolution("unresolved", error=error)
				binding_limits = (
					*limits,
					(module.path, (assignment.lineno, assignment.col_offset)),
				)
				if isinstance(value, (ast.Name, ast.Attribute)):
					return self.resolve_function_in(
						module, dotted_name(value).split("."), binding_limits, seen
					)
				if isinstance(value, (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple)):
					return FunctionResolution("non_function", error=error)
				return FunctionResolution("unresolved", error=error)
			try:
				binding = self.binding(module, body, parts[0], limits, seen)
			except ResolutionError:
				return FunctionResolution("unresolved", error=error)
			if binding is None:
				return FunctionResolution("unresolved", error=error)
			binding_limits = (
				*limits,
				(module.path, (binding.statement.lineno, binding.statement.col_offset)),
			)
			if isinstance(binding.node, ast.alias) and isinstance(binding.statement, ast.ImportFrom):
				provider_reference = module.import_reference(binding.statement)
				try:
					provider, remaining = self.source_module(provider_reference)
				except ResolutionError:
					return FunctionResolution("unresolved", error=error)
				if remaining:
					return FunctionResolution("unresolved", error=error)
				name = parts[0] if binding.node.name == "*" else binding.node.name
				return self.resolve_function_in(provider, [name], binding_limits, seen)
			return FunctionResolution("unresolved", error=error)
		if isinstance(symbol.node, FUNCTION_NODES):
			return FunctionResolution("function", symbol=symbol)
		return FunctionResolution("non_function", symbol=symbol)

	def resolve_from(self, module_reference: str, qualified_name: str) -> Symbol:
		"""Resolve a name from an explicitly identified source provider."""
		parts = qualified_name.split(".")
		if not all(part.isidentifier() for part in parts):
			raise ResolutionError(f"Invalid qualified Python name: {qualified_name!r}")
		module, _ = self.source_module(module_reference)
		return self.resolve_in(module, parts, (), frozenset())

	def resolve_registration(self, reference: str) -> Symbol:
		"""Frappe hook paths specify a module and its final attribute."""
		provider, separator, name = reference.rpartition(".")
		if not separator:
			raise ResolutionError(f"Invalid dotted Python reference: {reference!r}")
		return self.resolve_from(provider, name)

	def _class_namespace_reads(self, statement, tree, module_bindings, module):
		"""Allow an eager tuple of class keys filtered by the standard cached-property type."""
		if statement not in runtime_body(tree) or not isinstance(statement, (ast.Assign, ast.AnnAssign)):
			return {}
		targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
		if not all(isinstance(target, ast.Name) for target in targets):
			return {}
		builtins = {"tuple", "vars", "isinstance"}
		if (builtins | {"*"}).intersection(module_bindings):
			return {}
		consumer = statement.value
		if not (
			isinstance(consumer, ast.Call)
			and isinstance(consumer.func, ast.Name)
			and consumer.func.id == "tuple"
			and len(consumer.args) == 1
			and not consumer.keywords
			and isinstance(consumer.args[0], ast.GeneratorExp)
		):
			return {}
		generator = consumer.args[0]
		if len(generator.generators) != 1:
			return {}
		clause = generator.generators[0]
		if (
			clause.is_async
			or not isinstance(clause.target, ast.Tuple)
			or len(clause.target.elts) != 2
			or not all(isinstance(target, ast.Name) for target in clause.target.elts)
			or len(clause.ifs) != 1
		):
			return {}
		key, value = (target.id for target in clause.target.elts)
		if key == value or not isinstance(generator.elt, ast.Name) or generator.elt.id != key:
			return {}
		predicate = clause.ifs[0]
		if not (
			isinstance(predicate, ast.Call)
			and isinstance(predicate.func, ast.Name)
			and predicate.func.id == "isinstance"
			and len(predicate.args) == 2
			and not predicate.keywords
			and isinstance(predicate.args[0], ast.Name)
			and predicate.args[0].id == value
			and isinstance(predicate.args[1], ast.Name)
		):
			return {}
		type_name = predicate.args[1].id
		if {key, value}.intersection(builtins | {type_name}):
			return {}
		if not any(
			isinstance(import_node, ast.ImportFrom)
			and import_node.level == 0
			and import_node.module == "functools"
			and import_node.lineno < consumer.lineno
			and any(
				alias.name == "cached_property"
				and (alias.asname or alias.name) == type_name
				and module_bindings.get(type_name) == [alias]
				for alias in import_node.names
			)
			for import_node in runtime_body(tree)
		):
			return {}
		items = clause.iter
		if not (
			isinstance(items, ast.Call)
			and not items.args
			and not items.keywords
			and isinstance(items.func, ast.Attribute)
			and items.func.attr == "items"
		):
			return {}
		call = items.func.value
		if not (
			isinstance(call, ast.Call)
			and isinstance(call.func, ast.Name)
			and call.func.id == "vars"
			and len(call.args) == 1
			and not call.keywords
			and isinstance(call.args[0], ast.Name)
			and call.args[0].id not in {key, value}
		):
			return {}

		def ordinary_class(owner, *, ancestor=False):
			if not isinstance(owner, ast.ClassDef) or owner.decorator_list or owner.keywords:
				return None
			if ancestor and "__init_subclass__" in scope_bindings(runtime_body(tree, owner)):
				return None
			dependencies = {owner.name}
			for base in owner.bases:
				if not isinstance(base, ast.Name):
					return None
				if base.id == "object" and "object" not in module_bindings:
					dependencies.add("object")
					continue
				parent = unique_definition(tree, base.id)
				if not isinstance(parent, ast.ClassDef) or parent.end_lineno >= owner.lineno:
					return None
				inherited = ordinary_class(parent, ancestor=True)
				if inherited is None:
					return None
				dependencies.update(inherited)
			return dependencies

		owner = unique_definition(tree, call.args[0].id)
		dependencies = ordinary_class(owner)
		if dependencies and owner.end_lineno < call.lineno and module is not None:
			key = (tree, owner)
			if key not in self._verified_class_namespaces:
				if key in self._checking_class_namespaces:
					return {}
				self._checking_class_namespaces.add(key)
				try:
					if not class_values_are_safe(self, module, owner):
						return {}
					# Source trees stay immutable for this resolver, including revisions.
					self._verified_class_namespaces.add(key)
				finally:
					self._checking_class_namespaces.remove(key)
			return {
				call: dependencies
				| builtins
				| {type_name, "__class__", "__getattribute__", "__getattr__", "__slots__", "__new__"}
			}
		return {}

	def dynamic_namespace(self, scopes, names, tree, *, module=None):
		"""Reject unsupported namespace access without interpreting its effects."""
		namespace_calls = {"exec", "globals", "locals", "vars", "setattr", "delattr"}
		module_bindings = scope_bindings(runtime_body(tree))
		class_reads = {}
		if isinstance(tree, ast.Module) and not {"vars", "*"}.intersection(module_bindings):
			for statement in scopes[0]:
				class_reads.update(self._class_namespace_reads(statement, tree, module_bindings, module))
		names = set(names)
		if class_reads:
			names.add("vars")
			for dependencies in class_reads.values():
				names.update(dependencies)
		for scope in scopes:
			for statement in scope:
				for node in executable_nodes(statement, tree=tree):
					if (
						isinstance(node, ast.Call)
						and node not in class_reads
						and (
							(isinstance(node.func, ast.Name) and node.func.id in namespace_calls)
							or (isinstance(node.func, ast.Attribute) and node.func.attr in namespace_calls)
						)
					) or (
						isinstance(node, ast.Attribute)
						and (
							node.attr == "__dict__"
							or (class_reads and isinstance(node.ctx, (ast.Store, ast.Del)))
							or (
								(isinstance(node.ctx, (ast.Store, ast.Del)) or node.attr == "__all__")
								and node.attr in names
							)
						)
					):
						return True
					if isinstance(node, ast.ClassDef):
						body = runtime_body(tree, node)
						# Global declarations apply even inside a typing-only branch.
						global_names = {
							name
							for item in scope_statements(node.body)
							if isinstance(item, ast.Global)
							for name in item.names
						}
						bindings = scope_bindings(body)
						if global_names.intersection(names, bindings):
							return True
		return False

	def resolve_in(self, module, parts, limits, seen, *, dotted_path=False):
		body = self.module_body(module, limits)
		scopes = [body]
		key = (module.path, ".".join(parts), len(body), "resolve")
		if key in seen:
			raise ResolutionError(f"Ambiguous cyclic import for {module.name}.{'.'.join(parts)}")
		seen = seen | {key}
		decorators = overload_names(body)
		for index, name in enumerate(parts):
			if self.dynamic_namespace(scopes, parts, module.tree, module=module):
				raise ResolutionError(f"Ambiguous dynamic namespace in {module.path}")
			binding = self.binding(module, body, name, limits, seen, decorators)
			if index == 0 and len(parts) > 1 and module.path.name == "__init__.py":
				prefix = f"{module.name}.{name}"
				stem = module.app.package.joinpath(*prefix.split(".")[1:])
				if self.is_dir(stem) or self.is_file(stem.with_suffix(".py")):
					if binding is None:
						child, _ = self.source_module(prefix)
						try:
							return self.resolve_in(child, parts[1:], limits, seen, dotted_path=dotted_path)
						except MissingSymbolError as exc:
							exc.owner_resolved = True
							raise
					if dotted_path:
						raise ResolutionError(
							f"Ambiguous package attribute and child module {prefix}; identify the source provider explicitly"
						)
			if binding is None:
				if any(path == module.path for path, _ in limits):
					raise ResolutionError(
						f"Ambiguous circular import: {name!r} is not yet declared in {module.path}"
					)
				if "__getattr__" in scope_bindings(body) or (
					index == 0 and self.dynamic_namespace([body], {"__getattr__"}, module.tree, module=module)
				):
					raise ResolutionError(f"Ambiguous dynamic namespace in {module.path}")
				raise MissingSymbolError(
					f"Upstream symbol {module.name}.{'.'.join(parts)} not found in {module.path}; it may have been removed or renamed",
					owner_resolved=index > 0,
				)
			if isinstance(binding.node, ast.alias) and index == 0:
				if isinstance(binding.statement, ast.ImportFrom):
					reference = module.import_reference(binding.statement)
					tail = parts if binding.node.name == "*" else [binding.node.name, *parts[1:]]
				else:
					reference = module.import_reference(binding.statement, binding.node)
					tail = parts[1:]
					prefix = binding.node.name.split(".")
					# Dotted imports can bind a reexported attribute or the loaded
					# child module. Loading order is outside declaration analysis.
					if binding.node.asname or parts[: len(prefix)] == prefix:
						for end in range(1, len(prefix)):
							parent, _ = self.source_module(".".join(prefix[:end]))
							if (
								self.binding(
									parent, self.module_body(parent, limits), prefix[end], limits, seen
								)
								is not None
							):
								raise ResolutionError(
									f"Ambiguous dotted import at {module.path}:{binding.statement.lineno}: {binding.node.name}"
								)
				try:
					if isinstance(binding.statement, ast.Import) and not binding.node.asname:
						# A root package alone does not prove the imported child exists.
						self.source_module(binding.node.name)
					imported, _ = self.source_module(reference)
					return self.resolve_in(
						imported,
						tail,
						(*limits, (module.path, (binding.statement.lineno, binding.statement.col_offset))),
						seen,
						dotted_path=isinstance(binding.statement, ast.Import) and not binding.node.asname,
					)
				except ResolutionError as exc:
					if isinstance(exc, MissingSymbolError):
						if exc.owner_resolved or (
							isinstance(binding.statement, ast.Import) and binding.node.asname and tail
						):
							# The class or imported module exists; its member is absent.
							exc.owner_resolved = True
							raise
					raise ResolutionError(
						f"Ambiguous import at {module.path}:{binding.statement.lineno} ({'.'.join([reference, *tail])}): {exc}"
					) from exc
			if not isinstance(binding.node, DEFINITION_NODES):
				raise ResolutionError(
					f"Ambiguous non-definition {name!r} in {module.path}:{binding.statement.lineno}"
				)
			node = binding.node
			qualname = ".".join(parts[: index + 1])
			if self.require_unique_definitions and unique_definition(module.tree, qualname) is not node:
				raise ResolutionError(
					f"Ambiguous declarations for {module.name}.{qualname}; METHOD must identify one implementation"
				)
			if index == len(parts) - 1:
				return Symbol(module, qualname, node, limits)
			if not isinstance(node, ast.ClassDef):
				break
			body = runtime_body(module.tree, node)
			scopes.append(body)
			decorators |= overload_names(body)
		raise ResolutionError(f"Cannot resolve definition {module.name}.{'.'.join(parts)}")

	def base_classes(self, symbol: Symbol, *, ordinary_metaclass=False) -> list[Symbol]:
		if ordinary_metaclass and (
			symbol.node.decorator_list
			or symbol.node.keywords
			or self.dynamic_namespace(
				[runtime_body(symbol.module.tree), runtime_body(symbol.module.tree, symbol.node)],
				{"__bases__", "__class__", "__subclasscheck__"},
				symbol.module.tree,
				module=symbol.module,
			)
		):
			raise ResolutionError(f"Unverified metaclass behavior for {symbol.reference}")
		result = []
		for base in symbol.node.bases:
			name = dotted_name(base)
			first, *rest = name.split(".")
			limits = (
				*symbol.import_limits,
				(symbol.module.path, (symbol.node.lineno, symbol.node.col_offset)),
			)
			body = self.module_body(symbol.module, limits)
			binding = self.binding(symbol.module, body, first, limits, frozenset())
			if name == "object" and binding is None:
				continue
			if name == symbol.node.name and binding is None:
				raise ResolutionError(
					f"Cyclic inheritance or undeclared base {name!r} in {symbol.module.path}"
				)
			reference = f"{symbol.module.name}.{name}"
			if binding is not None and isinstance(binding.node, ast.alias):
				prefix = symbol.module.import_reference(binding.statement, binding.node)
				reference = ".".join([prefix, *(name.split(".") if binding.node.name == "*" else rest)])
			if reference in {"abc.ABC", "typing.Protocol", "typing_extensions.Protocol", "builtins.object"}:
				if ordinary_metaclass and reference != "builtins.object":
					raise ResolutionError(f"Unverified metaclass behavior for {symbol.reference}")
				continue
			resolved = self.resolve_in(symbol.module, name.split("."), limits, frozenset())
			if not isinstance(resolved.node, ast.ClassDef):
				raise ResolutionError(f"Base {reference} is not a class")
			result.append(resolved)
		return result

	def method(self, owner: Symbol, name: str):
		"""Inspect the selected class body, retaining module-level overload aliases."""
		module_body = self.module_body(owner.module, owner.import_limits)
		class_body = runtime_body(owner.module.tree, owner.node)
		if self.dynamic_namespace(
			[module_body, class_body],
			[
				*owner.qualname.split("."),
				name,
				"__code__",
				"__defaults__",
				"__kwdefaults__",
				"__annotations__",
			],
			owner.module.tree,
			module=owner.module,
		):
			raise ResolutionError(
				f"Ambiguous dynamic namespace in {owner.module.path} for {owner.reference}.{name}"
			)
		try:
			binding = select_binding(
				class_body,
				name,
				decorators=overload_names(module_body),
			)
		except AmbiguousBinding:
			return None
		return binding.node if binding and isinstance(binding.node, FUNCTION_NODES) else None

	def hierarchy(self, symbol: Symbol, seen: frozenset[DeclarationId] = frozenset()) -> list[Symbol]:
		"""Python C3 linearization, so multiple inheritance resolves the real owner."""
		if symbol.identity in seen:
			raise ResolutionError(f"Cyclic inheritance at {symbol.reference}")
		bases = self.base_classes(symbol)
		return [symbol, *self.compose(bases, seen | {symbol.identity})]

	def controller_variants(self, controller: Symbol) -> list[Symbol]:
		"""Resolve explicit class selections without executing a controller factory."""
		hierarchy = self.hierarchy(controller)
		owner = next(
			(
				s
				for s in hierarchy
				if "resolve_controller" in scope_bindings(runtime_body(s.module.tree, s.node))
			),
			None,
		)
		if owner is None:
			return []
		factory = self.method(owner, "resolve_controller")
		if (
			not isinstance(factory, ast.FunctionDef)
			or len(factory.decorator_list) != 1
			or not any(isinstance(d, ast.Name) and d.id == "classmethod" for d in factory.decorator_list)
		):
			raise ResolutionError(f"Cannot statically inspect {owner.reference}.resolve_controller")
		module_body = runtime_body(owner.module.tree)
		class_body = runtime_body(owner.module.tree, owner.node)
		if (
			self.binding(owner.module, module_body, "classmethod", (), frozenset()) is not None
			or {"classmethod", "*"}.intersection(scope_bindings(class_body))
			or self.dynamic_namespace(
				[module_body, class_body], {"classmethod"}, owner.module.tree, module=owner.module
			)
		):
			raise ResolutionError(f"Shadowed classmethod in {owner.reference}")
		body = factory.body
		bindings = scope_bindings(body)
		parameters = [*factory.args.posonlyargs, *factory.args.args]
		receiver = parameters[0].arg if parameters else None
		# A bare annotation creates a function local even though it assigns no value.
		for statement in scope_statements(body):
			if (
				isinstance(statement, ast.AnnAssign)
				and statement.value is None
				and isinstance(statement.target, ast.Name)
				and statement.target.id != receiver
			):
				bindings.setdefault(statement.target.id, [statement])
		if (
			len(parameters) != 1
			or factory.args.kwonlyargs
			or factory.args.vararg
			or factory.args.kwarg
			or bindings.get(receiver)
			or "*" in bindings
			or any(
				isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await))
				for statement in body
				for node in executable_nodes(statement)
			)
		):
			raise ResolutionError(f"Reassigned controller receiver in {owner.reference}.resolve_controller")
		imports = {
			alias: statement
			for statement in scope_statements(body)
			if isinstance(statement, (ast.Import, ast.ImportFrom))
			for alias in statement.names
		}
		controller_ids = {s.identity for s in hierarchy}
		referenced_classes = set()

		def resolve_class(name, position):
			referenced_classes.add(name)
			definitions = bindings.get(name, [])
			if definitions:
				alias = definitions[0]
				statement = imports.get(alias)
				if (
					len(definitions) != 1
					or not isinstance(statement, ast.ImportFrom)
					or statement not in body
					or statement.lineno >= position
					or alias.name == "*"
				):
					raise ResolutionError(f"Unavailable controller class {name!r} in {owner.reference}")
				symbol = self.resolve_registration(owner.module.import_reference(statement, alias))
			else:
				symbol = self.resolve_from(owner.module.name, name)
			if not isinstance(symbol.node, ast.ClassDef):
				raise ResolutionError(f"Controller implementation {symbol.reference} is not a class")
			return symbol

		def class_names(expression):
			if isinstance(expression, ast.Name):
				return [expression]
			if isinstance(expression, ast.IfExp):
				try:
					condition = bool(ast.literal_eval(expression.test))
				except ValueError, TypeError, SyntaxError:
					return class_names(expression.body) + class_names(expression.orelse)
				return class_names(expression.body if condition else expression.orelse)
			raise ResolutionError(f"Unsupported controller selection in {owner.reference}.resolve_controller")

		def subclass_names(expression):
			if isinstance(expression, ast.Name):
				return [expression]
			if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr):
				return subclass_names(expression.left) + subclass_names(expression.right)
			raise ResolutionError(f"Unsupported subclass guard in {owner.reference}.resolve_controller")

		def known_test(test):
			try:
				return bool(ast.literal_eval(test))
			except ValueError, TypeError, SyntaxError:
				pass
			if (
				isinstance(test, ast.Compare)
				and isinstance(test.left, ast.Name)
				and test.left.id == receiver
				and len(test.ops) == 1
				and isinstance(test.comparators[0], ast.Name)
				and isinstance(test.ops[0], (ast.Is, ast.IsNot))
			):
				target = resolve_class(test.comparators[0].id, test.lineno)
				matches = target.identity == controller.identity
				return not matches if isinstance(test.ops[0], ast.IsNot) else matches
			if (
				isinstance(test, ast.Call)
				and isinstance(test.func, ast.Name)
				and test.func.id == "issubclass"
				and len(test.args) == 2
				and not test.keywords
				and isinstance(test.args[0], ast.Name)
				and test.args[0].id == receiver
			):
				if (
					receiver == "issubclass"
					or "issubclass" in bindings
					or self.binding(owner.module, module_body, "issubclass", (), frozenset()) is not None
					or self.dynamic_namespace(
						[module_body, body], {"issubclass"}, owner.module.tree, module=owner.module
					)
				):
					raise ResolutionError(f"Shadowed issubclass in {owner.reference}")
				targets = [resolve_class(n.id, n.lineno) for n in subclass_names(test.args[1])]
				for target in targets:
					for ancestor in self.hierarchy(target):
						self.base_classes(ancestor, ordinary_metaclass=True)
				return any(target.identity in controller_ids for target in targets)
			return None

		returns = []

		def collect(statements):
			for statement in statements:
				if isinstance(statement, ast.Return):
					returns.append(statement)
					return True
				if isinstance(statement, ast.If):
					known = known_test(statement.test)
					if known is not None:
						if collect(statement.body if known else statement.orelse):
							return True
					else:
						left = collect(statement.body)
						right = collect(statement.orelse)
						if left and right:
							return True
				elif not isinstance(
					statement,
					(ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass, ast.Global),
				):
					raise ResolutionError(f"Unsupported controller control flow in {owner.reference}")
			return False

		if not collect(body) or not returns:
			raise ResolutionError(f"Cannot determine implementations of {owner.reference}.resolve_controller")
		variants = {}
		for statement in returns:
			value = statement.value
			if isinstance(value, ast.Name) and value.id == receiver:
				continue
			if (
				isinstance(value, ast.Name)
				and bindings.get(value.id)
				and not isinstance(bindings[value.id][0], ast.alias)
			):
				definitions = bindings[value.id]
				if (
					len(definitions) != 1
					or not isinstance(definitions[0], (ast.Assign, ast.AnnAssign))
					or definitions[0] not in body
					or definitions[0].lineno >= statement.lineno
				):
					raise ResolutionError(
						f"Unavailable controller selection {value.id!r} in {owner.reference}"
					)
				value = definitions[0].value
			for name in class_names(value):
				variant = resolve_class(name.id, name.lineno)
				if owner.identity not in {s.identity for s in self.hierarchy(variant)}:
					raise ResolutionError(
						f"Controller implementation {variant.reference} must inherit {owner.reference}"
					)
				if variant.identity != controller.identity:
					variants[variant.identity] = variant
		if self.dynamic_namespace(
			[body],
			set(bindings) | referenced_classes | {receiver, owner.node.name},
			owner.module.tree,
			module=owner.module,
		) or any(
			isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del))
			for statement in body
			for node in executable_nodes(statement, tree=owner.module.tree)
		):
			raise ResolutionError(f"Dynamic controller selection in {owner.reference}.resolve_controller")
		return list(variants.values())

	def compose(self, bases: list[Symbol], seen: frozenset[DeclarationId] = frozenset()) -> list[Symbol]:
		"""Linearize the bases of a synthetic Frappe extended controller."""
		sequences = [self.hierarchy(base, seen) for base in bases] + [bases[:]]
		result = []
		while any(sequences):
			sequences = [seq for seq in sequences if seq]
			candidate = next(
				(
					seq[0]
					for seq in sequences
					if all(seq[0].identity not in [s.identity for s in other[1:]] for other in sequences)
				),
				None,
			)
			if candidate is None:
				raise ResolutionError(
					f"Inconsistent inheritance for {', '.join(base.reference for base in bases)}"
				)
			result.append(candidate)
			for seq in sequences:
				if seq[0].identity == candidate.identity:
					seq.pop(0)
		return result

	def controller(self, doctype: str, custom_app: str) -> Symbol | None:
		scrubbed = doctype.lower().replace(" ", "_").replace("-", "_")
		found = []
		for app in self.apps.values():
			if app.name == custom_app:
				continue
			for path in sorted(app.package.glob(f"**/doctype/{scrubbed}/{scrubbed}.py")):
				metadata = path.with_suffix(".json")
				if metadata.is_file():
					try:
						data = json.loads(metadata.read_text(encoding="utf-8"))
						if not isinstance(data, dict):
							raise ValueError("DocType metadata must be a JSON object")
						if data.get("name") != doctype:
							continue
					except (OSError, ValueError) as exc:
						raise ResolutionError(f"Cannot read {metadata}: {exc}") from exc
				module_name = ".".join([app.name, *path.relative_to(app.package).with_suffix("").parts])
				class_name = doctype.replace(" ", "").replace("-", "")
				symbol = self.resolve_from(module_name, class_name)
				if not isinstance(symbol.node, ast.ClassDef):
					raise ResolutionError(
						f"Controller {module_name}.{class_name} not found; it may have been renamed"
					)
				found.append(symbol)
		if len(found) > 1:
			raise ResolutionError(
				f"Ambiguous upstream controller for {doctype}: {', '.join(s.reference for s in found)}"
			)
		return found[0] if found else None


@dataclass
class ValidationResult:
	errors: list[str] = field(default_factory=list)
	checked: int = 0


class AnnotationValidator:
	def __init__(self, app: App, resolver: SourceResolver):
		self.app = app
		self.resolver = resolver
		self.result = ValidationResult()
		self.checked: set[tuple[DeclarationId, DeclarationId]] = set()

	def error(self, custom: Symbol, upstream: str, message: str):
		context = getattr(self, "discovery_context", {})
		location = ""
		if context.get("kind") == "module_assignment":
			location = f"; installed at {self.app.root / context['file']}:{context['line']}"
		self.result.errors.append(
			f"{custom.module.path}:{custom.node.lineno}: {custom.reference} -> {upstream}: {message}{location}"
		)

	def validate_annotation(self, custom: Symbol, upstream: Symbol):
		key = (custom.identity, upstream.identity)
		if key in self.checked:
			return
		self.checked.add(key)
		self.result.checked += 1
		annotations, duplicates = parse_annotation(ast.get_docstring(custom.node) or "")
		field_errors = annotation_field_errors(annotations, duplicates, self.app.name)
		invalid_fields = set()
		for fields, message in field_errors:
			invalid_fields.update(fields)
			self.error(custom, upstream.reference, message)
		if annotations.get("REPO") and "REPO" not in invalid_fields:
			try:
				expected = upstream.module.app.repository
				if expected is None:
					raise ResolutionError(
						f"Cannot validate REPO: configure remote.origin.url in {upstream.module.app.root} or use owner/repository in required_apps"
					)
				expected_identity = repository_identity(expected)
				if repository_identity(annotations["REPO"]) != expected_identity:
					raise ResolutionError(f"REPO does not match the upstream checkout ({expected_identity})")
				github_repository(annotations["REPO"])
			except (ResolutionError, ValueError) as exc:
				self.error(custom, upstream.reference, str(exc))
		if annotations.get("PATH") and "PATH" not in invalid_fields:
			expected_path = upstream.module.path.relative_to(upstream.module.app.root).as_posix()
			path = PurePosixPath(annotations["PATH"])
			if path.is_absolute() or ".." in path.parts or annotations["PATH"] != expected_path:
				self.error(custom, upstream.reference, f"PATH must be {expected_path!r}")
		if annotations.get("METHOD") and "METHOD" not in invalid_fields:
			method = annotations["METHOD"]
			# Legacy leaf names are valid only when they identify this callable uniquely.
			valid = (
				method == upstream.qualname
				and unique_definition(upstream.module.tree, method) is upstream.node
			)
			if not valid and method == upstream.node.name:
				valid = find_function(upstream.module.tree, method) is upstream.node
			if not valid:
				self.error(
					custom,
					upstream.reference,
					f"METHOD must identify {upstream.qualname!r}; qualify class methods and avoid ambiguous declarations",
				)

	def function(self, original: str, replacement: str):
		custom = self.resolver.resolve_registration(replacement)
		try:
			if custom.module.app.name != self.app.name or not isinstance(custom.node, FUNCTION_NODES):
				raise ResolutionError("Override must resolve to a function in the custom app")
			upstream = self.resolver.resolve_registration(original)
			if upstream.module.app.name == self.app.name or not isinstance(upstream.node, FUNCTION_NODES):
				raise ResolutionError("Original must resolve to a function in a required app or frappe")
			self.validate_annotation(custom, upstream)
		except ResolutionError as exc:
			self.error(custom, original, str(exc))

	def doctype(self, doctype: str, replacement: str, *, extension: bool | None = None):
		custom = self.resolver.resolve_registration(replacement)
		try:
			if custom.module.app.name != self.app.name or not isinstance(custom.node, ast.ClassDef):
				raise ResolutionError("DocType override must resolve to a class in the custom app")
			hooks_path = self.app.package / "hooks.py"
			hooks = read_hooks(hooks_path) if hooks_path.is_file() else {}
			if extension is None:
				extension = (doctype, replacement) in hook_mapping(hooks, "extend_doctype_class")
			if extension:
				hierarchy = self.extension_hierarchy(doctype, custom, hooks)
			else:
				# A replacement class uses its declared bases; it does not inherit the
				# original controller merely because it is registered for that DocType.
				hierarchy = self.resolver.hierarchy(custom)
			upstream_classes = [s for s in hierarchy if s.module.app.name != self.app.name]
			if not upstream_classes:
				raise ResolutionError(
					f"Cannot resolve upstream controller or base class for DocType {doctype!r}"
				)
			controller = self.extension_controller(doctype, hooks) if extension else custom
			variant_hierarchies = [
				self.resolver.hierarchy(v) for v in self.resolver.controller_variants(controller)
			]
			# Include inherited methods from local mixins, not just the registered class.
			methods = {}
			class_bindings = {
				owner.identity: scope_bindings(runtime_body(owner.module.tree, owner.node))
				for owner in [*hierarchy, *(s for variant in variant_hierarchies for s in variant)]
			}
			for owner in hierarchy:
				for name, declarations in class_bindings[owner.identity].items():
					if name not in methods:
						methods[name] = (
							owner,
							Symbol(owner.module, f"{owner.qualname}.{name}", declarations[0]),
						)
			for name, (method_owner, method) in methods.items():
				if method.module.app.name != self.app.name:
					continue
				original = None
				upstream_owner = None
				for owner in upstream_classes:
					if name not in class_bindings[owner.identity]:
						continue
					upstream_owner = owner
					node = self.resolver.method(owner, name)
					if isinstance(node, FUNCTION_NODES):
						original = Symbol(owner.module, f"{owner.qualname}.{name}", node)
					# A descriptor or assignment shadows the same name on every ancestor.
					break
				definitions = [
					node
					for node in class_bindings[method_owner.identity][name]
					if isinstance(node, FUNCTION_NODES)
				]
				annotated = any(parse_annotation(ast.get_docstring(node) or "")[0] for node in definitions)
				if upstream_owner is None:
					candidates = {}
					unresolved = []
					for variant in variant_hierarchies:
						for owner in variant:
							if owner.module.app.name == self.app.name:
								continue
							if name not in class_bindings[owner.identity]:
								continue
							if static_attribute(
								class_bindings[method_owner.identity][name]
							) and static_attribute(class_bindings[owner.identity][name]):
								break
							node = self.resolver.method(owner, name)
							if isinstance(node, FUNCTION_NODES):
								symbol = Symbol(owner.module, f"{owner.qualname}.{name}", node)
								candidates[symbol.identity] = (owner, symbol)
							else:
								unresolved.append(owner)
							break
					if unresolved:
						self.error(
							method,
							f"{unresolved[0].reference}.{name}",
							f"Cannot resolve concrete implementation of overriding method {method.reference}",
						)
						continue
					if candidates:
						choices = list(candidates.values())
						if len(choices) > 1 and annotated:
							fields, _ = parse_annotation(ast.get_docstring(definitions[-1]) or "")
							choices = [
								(owner, symbol)
								for owner, symbol in choices
								if fields.get("METHOD") == symbol.qualname
								and fields.get("PATH")
								== symbol.module.path.relative_to(symbol.module.app.root).as_posix()
							]
						if len(choices) != 1 and annotated:
							self.error(
								method,
								f"DocType {doctype}",
								"Ambiguous controller implementation; specify its qualified METHOD and PATH",
							)
							continue
						upstream_owner, original = choices[0]
				if upstream_owner is None and not annotated:
					# Helper descriptors may define a getter and setter with the same name.
					continue
				if (
					upstream_owner is not None
					and static_attribute(class_bindings[method_owner.identity][name])
					and static_attribute(class_bindings[upstream_owner.identity][name])
				):
					# Ordinary data attributes do not become method overrides merely
					# because both the controller and customization declare them.
					continue
				concrete = self.resolver.method(method_owner, name)
				if concrete is None or (original is None and upstream_owner is not None):
					self.error(
						method,
						f"{upstream_owner.reference}.{name}" if upstream_owner else f"DocType {doctype}",
						f"Cannot resolve concrete implementation of overriding method {method.reference}",
					)
					continue
				method = Symbol(method.module, method.qualname, concrete)
				if original:
					self.validate_annotation(method, original)
				elif annotated:
					self.error(
						method,
						f"{upstream_classes[0].reference}.{name}",
						"Annotated upstream method no longer exists in the class hierarchy; it may have been removed or renamed",
					)
		except ResolutionError as exc:
			self.error(custom, f"DocType {doctype}", str(exc))

	def extension_hierarchy(self, doctype: str, custom: Symbol, hooks: dict) -> list[Symbol]:
		"""Compose known mixin order; fail closed when dependency app order is unknown."""
		controller = self.extension_controller(doctype, hooks)
		if controller is None:
			raise ResolutionError(f"Cannot resolve upstream controller for DocType {doctype!r}")
		paths = [value for key, value in hook_mapping(hooks, "extend_doctype_class") if key == doctype]
		dependency_extensions = []
		for app in self.resolver.apps.values():
			path = app.package / "hooks.py"
			if app.name == self.app.name or not path.is_file():
				continue
			registered = read_hooks(path, only={"extend_doctype_class"})
			matches = [
				value for key, value in hook_mapping(registered, "extend_doctype_class") if key == doctype
			]
			if matches:
				dependency_extensions.append(matches)
		if len(dependency_extensions) > 1:
			raise ResolutionError(
				f"Unresolved extension composition for {doctype}: installed dependency app order is unavailable"
			)
		mixins = [self.resolver.resolve_registration(path) for path in reversed(paths)]
		if not any(mixin.identity == custom.identity for mixin in mixins):
			raise ResolutionError(f"Extension {custom.reference} is not registered for DocType {doctype!r}")
		if dependency_extensions:
			mixins.extend(
				self.resolver.resolve_registration(path) for path in reversed(dependency_extensions[0])
			)
		if any(not isinstance(mixin.node, ast.ClassDef) for mixin in mixins):
			raise ResolutionError(f"Extensions for {doctype} must resolve to classes")
		return self.resolver.compose([*mixins, controller])

	def extension_controller(self, doctype: str, hooks: dict) -> Symbol | None:
		"""Frappe applies class replacement before composing extension mixins."""
		replacements = [
			value for key, value in hook_mapping(hooks, "override_doctype_class") if key == doctype
		]
		if not replacements:
			for app in self.resolver.apps.values():
				path = app.package / "hooks.py"
				if app.name == self.app.name or not path.is_file():
					continue
				registered = read_hooks(path, only={"override_doctype_class"})
				matches = [
					value
					for key, value in hook_mapping(registered, "override_doctype_class")
					if key == doctype
				]
				if matches:
					replacements.append(matches[-1])
			if len(set(replacements)) > 1:
				raise ResolutionError(
					f"Ambiguous replacement controller for {doctype}: installed app order is unavailable"
				)
		if replacements:
			controller = self.resolver.resolve_registration(replacements[-1])
			if not isinstance(controller.node, ast.ClassDef):
				raise ResolutionError(f"Replacement controller for {doctype} must resolve to a class")
			return controller
		return self.resolver.controller(doctype, self.app.name)


def hook_mapping(hooks: dict, name: str) -> list[tuple[str, str]]:
	mapping = hooks.get(name, {})
	if not isinstance(mapping, dict):
		raise ResolutionError(f"{name} must be a dictionary")
	result = []
	for key, values in mapping.items():
		if not isinstance(key, str) or not key:
			raise ResolutionError(f"{name} keys must be non-empty strings")
		if isinstance(values, str):
			values = [values]
		if not isinstance(values, (list, tuple)) or not all(
			isinstance(value, str) and value for value in values
		):
			raise ResolutionError(f"{name}[{key!r}] must contain dotted Python paths")
		result.extend((key, value) for value in values)
	return result


def dependency_apps(app: App, apps_root: Path, additional_dependencies=()) -> list[App]:
	"""Read required_apps transitively without importing any checkout."""
	apps = {app.name: app}
	pending = [app]
	while pending:
		current = pending.pop(0)
		hooks_path = current.package / "hooks.py"
		hooks = (
			read_hooks(hooks_path, only={"required_apps"})
			if hooks_path.is_file() and current.name != "frappe"
			else {}
		)
		required = hooks.get("required_apps", [])
		if isinstance(required, str):
			required = [required]
		if not isinstance(required, (list, tuple)):
			raise ResolutionError(f"{hooks_path}: required_apps must be a string, list or tuple of app names")
		if current.name == app.name:
			required = [*required, *additional_dependencies]
		dependencies = {"frappe": None}
		dependencies.update(normalize_required_app(value) for value in required)
		for name, repository in dependencies.items():
			if name in apps:
				if repository and not apps[name].fallback_repo:
					apps[name].fallback_repo = repository
				continue
			dependency = locate_app(apps_root / name, name, repository)
			apps[name] = dependency
			pending.append(dependency)
	return list(apps.values())


def registration_line(hooks_path, replacement):
	return next(
		(
			node.lineno
			for node in ast.walk(read_tree(hooks_path))
			if isinstance(node, ast.Constant) and node.value == replacement
		),
		1,
	)


def app_python_modules(app, resolver):
	"""Yield every Python module in an app package in stable source order."""
	for path in sorted(app.package.rglob("*.py")):
		if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(app.package).parts):
			continue
		parts = path.relative_to(app.root).with_suffix("").parts
		name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
		yield resolver.module(app, name, path)


def imported_owner_references(module, body, parts):
	"""Return dependency identities suggested by imports of an assignment owner."""
	result = []
	for statement in scope_statements(body):
		if not isinstance(statement, (ast.Import, ast.ImportFrom)):
			continue
		for alias in statement.names:
			local = alias.asname or (
				alias.name.split(".")[0] if isinstance(statement, ast.Import) else alias.name
			)
			if local != parts[0]:
				continue
			if isinstance(statement, ast.Import):
				if alias.asname:
					reference = [alias.name, *parts[1:]]
				else:
					imported = alias.name.split(".")
					if parts[: len(imported)] != imported:
						continue
					reference = parts
			else:
				reference = [module.import_reference(statement, alias), *parts[1:]]
			name = ".".join(reference)
			if name not in result:
				result.append(name)
	return result


def imported_owner_module(resolver, module, parts, limits):
	"""Resolve a module owner, retaining ambiguous dependency imports as findings."""
	owner = resolver.resolve_module_in(module, parts, limits, frozenset())
	if owner.state == "non_module":
		return None
	if owner.state == "module" and owner.module.app.name != module.app.name:
		return owner
	dependency_references = [
		reference
		for reference in owner.references
		if reference.split(".", 1)[0] in resolver.apps and reference.split(".", 1)[0] != module.app.name
	]
	if owner.state == "unresolved" and dependency_references:
		return resolver.module_resolution("unresolved", dependency_references, error=owner.error)
	return None


def assignment_parts(statement):
	if isinstance(statement, ast.Assign):
		if len(statement.targets) != 1:
			return None
		target = statement.targets[0]
	elif isinstance(statement, ast.AnnAssign):
		target = statement.target
	else:
		return None
	if not isinstance(target, ast.Attribute) or not isinstance(statement.value, (ast.Name, ast.Attribute)):
		return None
	try:
		return target, statement.value, dotted_name(target), dotted_name(statement.value)
	except ResolutionError:
		return None


def validate_module_assignments(app, validator, begin, fail):
	"""Discover direct module-function replacements without importing application code."""
	resolver = validator.resolver
	reported_unresolved = set()
	for module in app_python_modules(app, resolver):
		for statement in runtime_body(module.tree):
			parts = assignment_parts(statement)
			if parts is None:
				continue
			target, _, target_text, replacement_text = parts
			limits = ((module.path, (statement.lineno, statement.col_offset)),)
			replacement = resolver.resolve_function_in(
				module, replacement_text.split("."), limits, frozenset()
			)
			if replacement.state == "non_function":
				continue
			if replacement.state == "function" and replacement.symbol.module.app.name != app.name:
				continue
			owner = imported_owner_module(resolver, module, target_text.split(".")[:-1], limits)
			if owner is None:
				continue
			original_references = tuple(f"{reference}.{target.attr}" for reference in owner.references)
			original_reference = (
				original_references[0]
				if len(original_references) == 1
				else " | ".join(sorted(original_references))
			)
			context = {
				"kind": "module_assignment",
				"file": module.path.relative_to(app.root).as_posix(),
				"line": statement.lineno,
				"original_reference": target_text,
				"replacement_reference": replacement_text,
				"owner_module": owner.reference,
				"owner_candidates": list(owner.references),
				"registration_reference": original_reference,
				"registration_candidates": list(original_references),
			}
			if replacement.state == "function":
				context["_custom"] = replacement.symbol
			if owner.state == "unresolved":
				begin(context)
				fail(
					context,
					OwnerResolutionError(
						f"Cannot resolve monkey-patched owner module {owner.reference}: {owner.error}"
					),
				)
				continue
			original = resolver.resolve_function_in(owner.module, [target.attr], (), frozenset())
			if original.state == "non_function":
				continue

			begin(context)
			if replacement.state == "unresolved":
				fail(
					context,
					ResolutionError(f"Cannot resolve replacement function: {replacement.error}"),
				)
				continue
			if original.state == "unresolved":
				key = (replacement.symbol.identity, context["file"], original_reference)
				if key in reported_unresolved:
					continue
				reported_unresolved.add(key)
				validator.error(
					replacement.symbol,
					original_reference,
					f"Cannot resolve monkey-patched original at {context['file']}:{context['line']}: {original.error}",
				)
				continue
			validator.validate_annotation(replacement.symbol, original.symbol)


def run_override_validation(
	app: App,
	apps: list[App],
	validator: AnnotationValidator,
	*,
	before_registration=None,
	resolution_error=None,
):
	"""Run every supported discovery source through one configured validator."""
	hooks_path = app.package / "hooks.py"
	hooks = read_hooks(hooks_path)

	def begin(context):
		validator.discovery_context = context
		if before_registration:
			before_registration(context)

	def fail(context, exc):
		if resolution_error:
			resolution_error(context, exc)
		elif context["kind"] == "hook":
			validator.result.errors.append(
				f"{hooks_path}: {context['hook']}[{context['original_reference']!r}] -> "
				f"{context['replacement_reference']}: {exc}"
			)
		else:
			validator.result.errors.append(
				f"{app.root / context['file']}:{context['line']}: "
				f"{context['original_reference']} -> {context['replacement_reference']}: {exc}"
			)

	for hook in ("override_whitelisted_methods", "override_doctype_class", "extend_doctype_class"):
		for original, replacement in hook_mapping(hooks, hook):
			context = {
				"kind": "hook",
				"hook": hook,
				"file": hooks_path.relative_to(app.root).as_posix(),
				"line": registration_line(hooks_path, replacement),
				"original_reference": original,
				"replacement_reference": replacement,
			}
			begin(context)
			try:
				if hook == "override_whitelisted_methods":
					validator.function(original, replacement)
				else:
					validator.doctype(original, replacement, extension=hook == "extend_doctype_class")
			except ResolutionError as exc:
				fail(context, exc)
	validate_module_assignments(app, validator, begin, fail)
	return validator.result


def validate_overrides(
	app_path: Path, apps_root: Path | None = None, additional_dependencies=()
) -> ValidationResult:
	"""Validate a repository/package using sibling checkouts; no network or imports."""
	result = ValidationResult()
	try:
		app = locate_app(app_path)
		hooks_path = app.package / "hooks.py"
		if not hooks_path.is_file():
			raise ResolutionError(f"hooks.py not found in {app.package}")
		root = apps_root.resolve() if apps_root else app.root.parent
		apps = dependency_apps(app, root, additional_dependencies)
		validator = AnnotationValidator(app, SourceResolver(apps))
		return run_override_validation(app, apps, validator)
	except ResolutionError as exc:
		result.errors.append(str(exc))
	return result
