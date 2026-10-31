"""Discover Frappe overrides and validate upstream annotations using local ASTs."""

import ast
import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path, PurePosixPath

from test_utils.utils.override_metadata import (
	FIELDS,
	github_repository,
	parse_annotation,
	repository_identity,
)
from test_utils.utils.python_scope import SourceValue
from test_utils.utils.python_source import (
	FUNCTION_NODES,
	AmbiguousBinding,
	Binding,
	SourceAnalyzer,
	definition_expressions,
	executable_nodes,
	mutable_objects,
	runtime_body,
	scope_bindings,
	scope_statements,
	select_binding,
	static_attribute,
	static_exports,
)

HOOKS = {"required_apps", "override_whitelisted_methods", "override_doctype_class", "extend_doctype_class"}


class ResolutionError(ValueError):
	"""An override cannot be verified from the configured checkouts."""


class MissingSymbol(ResolutionError):
	"""The inspected namespace provably has no declaration for this symbol."""


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

	@cached_property
	def imports(self) -> dict[str, str]:
		result = {}
		for node in scope_statements(self.tree.body):
			if isinstance(node, ast.Import):
				for alias in node.names:
					result[alias.asname or alias.name.split(".")[0]] = (
						alias.name if alias.asname else alias.name.split(".")[0]
					)
			elif isinstance(node, ast.ImportFrom):
				prefix = node.module or ""
				if node.level:
					package = (
						self.name.split(".") if self.path.name == "__init__.py" else self.name.split(".")[:-1]
					)
					prefix = ".".join(package[: len(package) - node.level + 1] + ([prefix] if prefix else []))
				for alias in node.names:
					if alias.name != "*":
						result[alias.asname or alias.name] = f"{prefix}.{alias.name}"
		return result


@dataclass
class Symbol:
	module: Module
	qualname: str
	node: ast.AST
	import_snapshot: tuple[tuple[str, int], ...] = ()
	import_effects: tuple = ()

	@property
	def reference(self) -> str:
		return f"{self.module.name}.{self.qualname}"

	@property
	def identity(self):
		return self.module.path, id(self.node)


class ImportAnalyzer(SourceAnalyzer):
	"""Retain checkout import identities, including a selected wildcard export."""

	def __init__(self, resolver, module):
		super().__init__()
		self.resolver = resolver
		self.module = module
		self.readonly_default_calls = set()
		self.readonly_default_nodes = set()

	@staticmethod
	def imported_references(value):
		return value.modules | frozenset(
			origin[1]
			for origin in value.origins
			if isinstance(origin, tuple)
			and len(origin) == 2
			and origin[0] == "import"
			and isinstance(origin[1], str)
		)

	@classmethod
	def imported_defaults(cls, value):
		return {
			reference
			for reference in cls.imported_references(value)
			if {"__defaults__", "__kwdefaults__"}.intersection(reference.split("."))
		}

	@staticmethod
	def call_location(node):
		return tuple(
			getattr(node, field, None) for field in ("lineno", "col_offset", "end_lineno", "end_col_offset")
		)

	def emit(self, effect):
		super().emit(effect)
		if effect.opaque and (
			self.call_location(effect.node) in self.readonly_default_calls
			or id(effect.node) in self.readonly_default_nodes
		):
			return
		if effect.mutation or effect.opaque or effect.namespace:
			value = SourceValue(effect.node, origins=effect.origins)
			references = self.imported_defaults(value)
			if effect.opaque or effect.namespace:
				references |= self.imported_references(
					SourceValue(effect.node, origins=effect.argument_origins)
				)
			for reference in references:
				self.module_writes.setdefault(reference, set()).add(None)

	def bind(self, target, value, scope, seen=frozenset(), *, anchor=True):
		if isinstance(target, ast.Subscript):
			for reference in self.imported_defaults(self.value(target.value, scope)):
				self.module_writes.setdefault(reference, set()).add(None)
		return super().bind(target, value, scope, seen, anchor=anchor)

	def expression(self, node, scope, seen):
		copied = None
		readonly = False
		readonly_nodes = []
		readonly_locations = []
		constructors = {
			"builtins.dict",
			"builtins.list",
			"builtins.tuple",
			"builtins.set",
			"builtins.frozenset",
		}
		if isinstance(node, ast.Call) and node.args and self.value(node.func, scope).modules & constructors:
			argument = self.value(node.args[0], scope)
			if any(
				reference.endswith((".__defaults__", ".__kwdefaults__")) for reference in argument.modules
			):
				readonly_nodes = [argument.expression, node.args[0]]
				readonly_locations = [
					self.call_location(item) for item in readonly_nodes if hasattr(item, "lineno")
				]
		if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
			base = self.value(node.func.value, scope)
			references = self.imported_defaults(base)
			readonly = bool(references) and node.func.attr in {
				"get",
				"keys",
				"values",
				"items",
				"copy",
				"__getitem__",
			}
			if readonly and node.func.attr in {"get", "__getitem__"}:
				key = self.value(node.args[0], scope) if node.args else None
				readonly = key is not None and key.scalar and not key.uncertain
			if node.func.attr == "copy" and references:
				copied = SourceValue(
					node,
					items={None: SourceValue(node, modules=frozenset(references), origins=base.origins)},
					origins=frozenset({node}),
				)
			elif node.func.attr not in {"get", "keys", "values", "items", "copy", "__getitem__"}:
				for reference in references:
					self.module_writes.setdefault(reference, set()).add(None)
		location = self.call_location(node)
		if readonly:
			self.readonly_default_calls.add(location)
		self.readonly_default_calls.update(readonly_locations)
		self.readonly_default_nodes.update(map(id, readonly_nodes))
		try:
			result = super().expression(node, scope, seen)
		finally:
			if readonly:
				self.readonly_default_calls.discard(location)
			self.readonly_default_calls.difference_update(readonly_locations)
			self.readonly_default_nodes.difference_update(map(id, readonly_nodes))
		if isinstance(node, ast.Call) and node.args and self.value(node.func, scope).modules & constructors:
			original = self.value(node.args[0], scope)
			references = self.imported_defaults(original)
			if references and original.items is None:
				current = self.call_values.get(node, SourceValue(node, origins=frozenset({node})))
				items = dict(current.items or {})
				items[None] = SourceValue(node, modules=frozenset(references), origins=original.origins)
				copied = replace(current, items=items)
		if copied is not None:
			self.call_values[node] = copied
		return result

	def visit(self, node, scope, seen):
		original = node
		wildcard = isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
		previous = scope.values.copy() if wildcard else None
		if isinstance(node, ast.ImportFrom) and node.level:
			node = ast.copy_location(
				ast.ImportFrom(
					module=self.resolver.import_module_name(self.module, node), names=node.names, level=0
				),
				node,
			)
		result = super().visit(node, scope, seen)
		if wildcard:
			references = self.resolver.known_wildcard_references(self.module, original)
			if references is None:
				return result
			for name, value in previous.items():
				if name not in references:
					scope.values[name] = value
			for name, reference in references.items():
				scope.set(
					name,
					SourceValue(
						ast.Name(id=name, ctx=ast.Load()),
						modules=frozenset({reference}),
						origins=frozenset({("import", reference)}),
					),
				)
		return result


class SourceResolver:
	def __init__(self, apps: list[App]):
		self.apps = {app.name: app for app in apps}
		self.modules: dict[Path, Module] = {}
		self.import_limits: dict[str, list[int]] = {}
		self.analyses = {}
		self.wildcard_references = {}
		self.mutation_resolution_depth = 0

	def module(self, app: App, name: str, path: Path) -> Module:
		if path not in self.modules:
			self.modules[path] = Module(app, name, path, read_tree(path))
		return self.modules[path]

	def resolve(self, reference: str, seen=frozenset()) -> Symbol:
		parts = reference.split(".")
		if not all(part.isidentifier() for part in parts):
			raise ResolutionError(f"Invalid dotted Python reference: {reference!r}")
		app = self.apps.get(parts[0])
		if app is None:
			raise ResolutionError(
				f"Cannot resolve {reference}: app {parts[0]!r} is not in required_apps (or frappe)"
			)
		for end in range(len(parts), 0, -1):
			stem = app.package.joinpath(*parts[1:end])
			for path in (stem.with_suffix(".py"), stem / "__init__.py"):
				if not path.is_file():
					continue
				module = self.module(app, ".".join(parts[:end]), path)
				remaining = parts[end:]
				if not remaining:
					continue
				value = self.qualified(module, ".".join(remaining), seen=seen)
				if value is not None:
					return value
				raise MissingSymbol(
					f"Upstream symbol {reference} not found in {path}; it may have been removed or renamed"
				)
		raise ResolutionError(
			f"Cannot resolve {reference}: source file not found; check --apps-root and upstream renames"
		)

	def import_module_name(self, module, statement):
		if not statement.level:
			return statement.module or ""
		package = module.name.split(".") if module.path.name == "__init__.py" else module.name.split(".")[:-1]
		if statement.level > len(package):
			raise ResolutionError(f"Invalid relative import in {module.path}:{statement.lineno}")
		return ".".join(
			package[: len(package) - statement.level + 1] + ([statement.module] if statement.module else [])
		)

	def imported_module(self, name):
		parts = name.split(".")
		app = self.apps.get(parts[0])
		if app is None:
			raise ResolutionError(f"No source checkout for imported module {name}")
		stem = app.package.joinpath(*parts[1:])
		for path in (stem.with_suffix(".py"), stem / "__init__.py"):
			if path.is_file():
				return self.module(app, name, path)
		raise ResolutionError(f"Source not found for imported module {name}")

	def module_limit(self, module, before=None):
		limits = self.import_limits.get(module.name, [])
		return min([*limits, *([before] if before is not None else [])], default=None)

	def module_body(self, module, before=None):
		limit = self.module_limit(module, before)
		body = [node for node in module.tree.body if limit is None or node.lineno < limit]
		return list(runtime_body(body)), limit

	def analysis(self, module, before=None):
		key = module.path, self.module_limit(module, before)
		if key not in self.analyses:
			body, _ = self.module_body(module, before)
			analyzer = ImportAnalyzer(self, module)
			for statement in body:
				analyzer.scan(statement)
			self.analyses[key] = analyzer
		return self.analyses[key]

	def class_body(self, owner):
		bodies = self.analysis(owner.module).runtime_bodies
		return bodies[owner.node] if owner.node in bodies else list(runtime_body(owner.node.body))

	def known_wildcard_references(self, module, statement):
		context = tuple(sorted((name, min(limits)) for name, limits in self.import_limits.items() if limits))
		key = module.path, id(statement), context
		if key in self.wildcard_references:
			return self.wildcard_references[key]
		limits = self.import_limits.setdefault(module.name, [])
		limits.append(statement.lineno)
		try:
			name = self.import_module_name(module, statement)
			imported = self.imported_module(name)
			body, limit = self.module_body(imported)
			exports = self.exported_names(imported, body, frozenset({(imported.name, "__all__", limit)}))
			names = (
				exports
				if exports is not None
				else {
					binding
					for binding in scope_bindings(body)
					if not binding.startswith("_") and binding != "*"
				}
			)
			if exports is None:
				for node in scope_statements(body):
					if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
						transferred = self.known_wildcard_references(imported, node)
						if transferred is None:
							self.wildcard_references[key] = None
							return None
						names.update(binding for binding in transferred if not binding.startswith("_"))
			result = {binding: f"{name}.{binding}" for binding in names}
		except ResolutionError, AmbiguousBinding:
			result = None
		finally:
			limits.pop()
		self.wildcard_references[key] = result
		return result

	def wildcard(self, module, statement, name, seen, *, before=None, remaining=()):
		imported_name = self.import_module_name(module, statement)
		importer = self.analysis(module, before)
		limits = self.import_limits.setdefault(module.name, [])
		limits.append(statement.lineno)
		try:
			imported = self.imported_module(imported_name)
			body, _ = self.module_body(imported)
			exports = self.exported_names(imported, body, seen)
			if (exports is not None and name not in exports) or (exports is None and name.startswith("_")):
				return None
			if exports is None and "__getattr__" in scope_bindings(body):
				raise AmbiguousBinding(f"dynamic exports via __getattr__ in {imported.path}")
			value = self.qualified(imported, name, seen=seen)
			if value is None and exports is not None:
				raise AmbiguousBinding(f"__all__ exports missing symbol {name} in {imported.path}")
			if value is not None:
				reference = f"{imported_name}.{name}"
				checked = (
					self.qualified(value.module, ".".join(remaining), owner=value, seen=seen)
					if remaining and isinstance(value.node, ast.ClassDef)
					else value
				)
				if checked is not None:
					checked = self.check_imported_mutations(
						module, ".".join([reference, *remaining]), checked, importer
					)
					if not remaining:
						value = checked
			return value
		except (ResolutionError, AmbiguousBinding) as exc:
			raise ResolutionError(
				f"Ambiguous resolution of {name}: wildcard import from {imported_name} "
				f"at {module.path}:{statement.lineno}: {exc}"
			) from exc
		finally:
			limits.pop()

	def exported_names(self, module, body, seen):
		def transfers_all(statement):
			limits = self.import_limits.setdefault(module.name, [])
			limits.append(statement.lineno)
			try:
				imported = self.imported_module(self.import_module_name(module, statement))
				imported_body, limit = self.module_body(imported)
				key = (imported.name, "__all__", limit)
				if key in seen:
					raise AmbiguousBinding(f"cyclic __all__ exports in {imported.path}")
				exports = self.exported_names(imported, imported_body, seen | {key})
				return exports is not None and "__all__" in exports
			finally:
				limits.pop()

		try:
			return static_exports(body, wildcard=transfers_all)
		except AmbiguousBinding as exc:
			raise AmbiguousBinding(f"{module.path}: {exc}") from exc

	def qualified(
		self,
		module,
		qualname,
		*,
		seen=frozenset(),
		before=None,
		allow_standard=False,
		owner=None,
		complete_importer=False,
	):
		body, limit = self.module_body(module, before)
		key = (module.name, qualname, limit, id(owner.node) if owner else None)
		if key in seen:
			raise ResolutionError(f"Ambiguous cyclic import while resolving {module.name}.{qualname}")
		seen = seen | {key}
		parts = qualname.split(".")
		prefix = f"{owner.qualname}." if owner else ""
		current_owner = owner
		if owner is not None:
			body = self.class_body(owner)
		for index, name in enumerate(parts):
			if current_owner is not None:
				writes = self.class_mutations(current_owner, before)
				if name in writes or None in writes:
					local = self.analysis(current_owner.module, before).member_writes.get(
						current_owner.node, ()
					)
					if current_owner.import_effects and name not in local and None not in local:
						raise ResolutionError(
							f"Ambiguous imported binding {current_owner.reference}.{name}: its member was mutated in an importer"
						)
					raise ResolutionError(
						f"Ambiguous mutated member {current_owner.reference}.{name} at {current_owner.module.path}"
					)
			try:
				binding = select_binding(
					body,
					name,
					wildcard=lambda statement, target: self.wildcard(
						module,
						statement,
						target,
						seen,
						before=None if complete_importer else before,
						remaining=parts[index + 1 :],
					),
					overloads=self.analysis(module, before).overload_definitions,
				)
			except AmbiguousBinding as exc:
				raise ResolutionError(
					f"Ambiguous resolution of {module.name}.{qualname} at {module.path}: {exc}"
				) from exc
			if binding is None and current_owner is not None:
				binding = self.inherited_member(current_owner, name, seen)
			if binding is None:
				if allow_standard and name == "object" and index == 0:
					return "builtins.object"
				if self.import_limits.get(module.name):
					raise ResolutionError(
						f"Ambiguous cyclic import: {module.name}.{name} is not yet bound before line {limit}"
					)
				return None
			if not isinstance(binding, Binding):
				value = binding
			elif isinstance(binding.node, ast.alias):
				statement = binding.statement
				alias = binding.node
				if isinstance(statement, ast.ImportFrom):
					reference = f"{self.import_module_name(module, statement)}.{alias.name}"
				else:
					reference = alias.name if alias.asname else alias.name.split(".")[0]
				reference = ".".join([reference, *parts[index + 1 :]])
				if allow_standard and reference in {
					"abc.ABC",
					"typing.Protocol",
					"typing_extensions.Protocol",
					"builtins.object",
				}:
					return reference
				if isinstance(statement, ast.ImportFrom):
					imported_name = self.import_module_name(module, statement)
					parts_from = imported_name.split(".")
					app_from = self.apps.get(parts_from[0])
					if app_from is not None:
						stem_from = app_from.package.joinpath(*parts_from[1:])
						if not stem_from.with_suffix(".py").is_file() and not stem_from.is_dir():
							raise ResolutionError(
								f"Source not found for imported module {imported_name} while resolving {reference} at {module.path}:{statement.lineno}"
							)
				limits = self.import_limits.setdefault(module.name, [])
				importer = self.analysis(module, None if complete_importer else before)
				limits.append(statement.lineno)
				try:
					value = self.resolve(reference, seen)
				finally:
					limits.pop()
				value = self.check_imported_mutations(module, reference, value, importer)
				return self.apply_member_effects(current_owner, ".".join(parts[index:]), value)
			elif isinstance(binding.node, (ast.ClassDef, *FUNCTION_NODES)):
				if binding.node in self.analysis(module, before).invalid_definitions:
					raise ResolutionError(
						f"Ambiguous modified or decorated definition {module.name}.{qualname} at {module.path}:{binding.node.lineno}"
					)
				value = Symbol(
					module,
					prefix + name,
					binding.node,
					tuple(
						sorted((name, min(limits)) for name, limits in self.import_limits.items() if limits)
					),
				)
			else:
				raise ResolutionError(
					f"Ambiguous binding of {module.name}.{qualname} at {module.path}:{binding.node.lineno}; unsupported assignment or deletion"
				)
			value = self.apply_member_effects(current_owner, name, value)
			if index == len(parts) - 1:
				return value
			if not isinstance(value.node, ast.ClassDef):
				return None
			if module.path != value.module.path:
				before = None
			module = value.module
			current_owner = value
			prefix = value.qualname + "."
			body = self.class_body(value)
		return None

	def apply_member_effects(self, owner, name, value):
		if owner is not None:
			for importer, _reference, analyzer in owner.import_effects:
				value = self.check_imported_mutations(importer, f"{owner.reference}.{name}", value, analyzer)
		return value

	def check_imported_mutations(self, module, reference, value, analyzer):
		"""An importer cannot reuse metadata after changing its imported object."""
		if self.mutation_resolution_depth or not analyzer.module_writes:
			return value
		sensitive = {"__code__", "__defaults__", "__kwdefaults__", "__doc__", "__wrapped__"}
		references = {reference: isinstance(value.node, ast.ClassDef)}
		owners = self.hierarchy(value) if isinstance(value.node, ast.ClassDef) else [value]
		references.update((owner.reference, isinstance(owner.node, ast.ClassDef)) for owner in owners)
		writes = self.imported_mutations(analyzer)
		for candidate, is_class in references.items():
			for owner, attributes in writes.items():
				if candidate == owner:
					affected = bool(attributes & ({"__bases__", None} if is_class else sensitive | {None}))
				elif candidate.startswith(owner + "."):
					affected = candidate[len(owner) + 1 :].split(".")[0] in attributes or None in attributes
				elif owner.startswith(candidate + "."):
					affected = not is_class and owner[len(candidate) + 1 :].split(".")[0] in sensitive
				else:
					affected = False
				if affected:
					raise ResolutionError(
						f"Ambiguous imported binding {reference} at {module.path}: its object was mutated in this importer"
					)
		return (
			self.with_import_effects(value, ((module, reference, analyzer),))
			if isinstance(value.node, ast.ClassDef)
			else value
		)

	def imported_mutations(self, analyzer):
		writes = {owner: set(attributes) for owner, attributes in analyzer.module_writes.items()}
		for owner, attributes in list(writes.items()):
			canonical = self.canonical_mutation_reference(owner)
			if canonical is not None:
				writes.setdefault(canonical, set()).update(attributes)
		return writes

	@staticmethod
	def with_import_effects(value, effects):
		merged = {
			(module.path, reference, id(analyzer)): (module, reference, analyzer)
			for module, reference, analyzer in (*value.import_effects, *effects)
		}
		return replace(value, import_effects=tuple(merged.values()))

	def class_mutations(self, owner, before=None):
		writes = set(self.analysis(owner.module, before).member_writes.get(owner.node, ()))
		for _, _, analyzer in owner.import_effects:
			writes.update(self.imported_mutations(analyzer).get(owner.reference, ()))
		return writes

	def canonical_mutation_reference(self, reference):
		"""Follow visible re-exports without recursively checking their importer effects."""
		parts = reference.split(".")
		if parts[0] not in self.apps:
			return None
		self.mutation_resolution_depth += 1
		try:
			for end in range(len(parts), 0, -1):
				try:
					owner = self.resolve(".".join(parts[:end]))
				except ResolutionError:
					continue
				return ".".join([owner.reference, *parts[end:]])
		finally:
			self.mutation_resolution_depth -= 1
		return None

	def base_classes(self, symbol: Symbol) -> list[Symbol]:
		"""Keep the partially initialized modules captured when this class was imported."""
		for name, limit in symbol.import_snapshot:
			self.import_limits.setdefault(name, []).append(limit)
		try:
			return self._base_classes(symbol)
		finally:
			for name, _ in reversed(symbol.import_snapshot):
				self.import_limits[name].pop()

	def _base_classes(self, symbol: Symbol) -> list[Symbol]:
		result = []
		for base in symbol.node.bases:
			name = dotted_name(base)
			resolved = self.qualified(
				symbol.module, name, before=symbol.node.lineno, allow_standard=True, complete_importer=True
			)
			if isinstance(resolved, str):
				continue
			if resolved is None and name == symbol.node.name:
				raise ResolutionError(f"Cyclic inheritance or unbound self-reference at {symbol.reference}")
			if resolved is None or not isinstance(resolved.node, ast.ClassDef):
				raise ResolutionError(f"Base {name} is not a class in {symbol.module.path}")
			result.append(self.with_import_effects(resolved, symbol.import_effects))
		return result

	def member(self, owner, name):
		"""Resolve against the selected class, preserving imported member provenance."""
		return self.qualified(owner.module, name, owner=owner)

	def inherited_member(self, owner, name, seen):
		"""Visit declarations in C3 order without following an ancestor's inherited lookup."""
		for ancestor in self.hierarchy(owner)[1:]:
			writes = self.class_mutations(ancestor)
			if name in scope_bindings(self.class_body(ancestor)) or name in writes or None in writes:
				return self.qualified(ancestor.module, name, owner=ancestor, seen=seen)
		return None

	def function_symbol(self, module, name):
		value = self.qualified(module, name)
		if value is not None:
			return value if isinstance(value.node, FUNCTION_NODES) else None
		if "." in name:
			return None
		candidates = {}
		visited = set()

		def relevant(node, current_module):
			body = self.analysis(current_module).runtime_bodies.get(node, node.body)
			return name in scope_bindings(body) or any(
				isinstance(child, ast.ClassDef) and relevant(child, current_module)
				for child in scope_statements(body)
			)

		def collect(owner=None):
			current_module = owner.module if owner else module
			body = self.class_body(owner) if owner else self.module_body(module)[0]
			class_names = dict.fromkeys(
				node.name
				for node in scope_statements(body)
				if isinstance(node, ast.ClassDef) and relevant(node, current_module)
			)
			for class_name in class_names:
				try:
					binding = select_binding(
						body, class_name, overloads=self.analysis(current_module).overload_definitions
					)
				except AmbiguousBinding:
					binding = None  # Qualified resolution can inspect a wildcard's actual exports.
				if isinstance(binding, Binding) and not isinstance(binding.node, ast.ClassDef):
					continue
				active = self.qualified(current_module, class_name, owner=owner)
				if active is None or not isinstance(active.node, ast.ClassDef) or active.identity in visited:
					continue
				visited.add(active.identity)
				if name in scope_bindings(self.class_body(active)):
					value = self.member(active, name)
					if value is not None and isinstance(value.node, FUNCTION_NODES):
						candidates[value.identity] = value
				collect(active)

		collect()
		return next(iter(candidates.values())) if len(candidates) == 1 else None

	def function_node(self, module, name):
		value = self.function_symbol(module, name)
		return value.node if value is not None else None

	def hierarchy(self, symbol: Symbol, seen: frozenset[tuple] = frozenset()) -> list[Symbol]:
		"""Python C3 linearization, so multiple inheritance resolves the real owner."""
		if symbol.identity in seen:
			raise ResolutionError(f"Cyclic inheritance at {symbol.reference}")
		bases = self.base_classes(symbol)
		return [symbol, *self.compose(bases, seen | {symbol.identity})]

	def compose(self, bases: list[Symbol], seen: frozenset[tuple] = frozenset()) -> list[Symbol]:
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
			candidate = self.with_import_effects(
				candidate,
				(
					effect
					for sequence in sequences
					for value in sequence
					if value.identity == candidate.identity
					for effect in value.import_effects
				),
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
				module = self.module(app, module_name, path)
				class_name = doctype.replace(" ", "").replace("-", "")
				value = self.qualified(module, class_name)
				if value is None or not isinstance(value.node, ast.ClassDef):
					raise ResolutionError(
						f"Controller {module_name}.{class_name} not found; it may have been renamed"
					)
				found.append(value)
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
		self.checked: set[tuple] = set()

	def error(self, custom: Symbol, upstream: str, message: str):
		self.result.errors.append(
			f"{custom.module.path}:{custom.node.lineno}: {custom.reference} -> {upstream}: {message}"
		)

	def validate_annotation(self, custom: Symbol, upstream: Symbol):
		key = (custom.identity, upstream.identity)
		if key in self.checked:
			return
		self.checked.add(key)
		self.result.checked += 1
		annotations, duplicates = parse_annotation(ast.get_docstring(custom.node) or "")
		for name in duplicates:
			self.error(custom, upstream.reference, f"Duplicate annotation field {name}")
		missing = [name for name in FIELDS if not annotations.get(name)]
		if missing:
			self.error(
				custom,
				upstream.reference,
				f"Missing annotation fields: {', '.join(missing)}; add them to the overriding callable's docstring",
			)
		if annotations.get("APP") and annotations["APP"] != self.app.name:
			self.error(custom, upstream.reference, f"APP must be {self.app.name!r}")
		if annotations.get("HASH") and not re.fullmatch(r"[0-9a-fA-F]{40}", annotations["HASH"]):
			self.error(custom, upstream.reference, "HASH must be a full 40-character hexadecimal commit SHA")
		if annotations.get("REPO"):
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
		if annotations.get("PATH"):
			expected_path = upstream.module.path.relative_to(upstream.module.app.root).as_posix()
			path = PurePosixPath(annotations["PATH"])
			if path.is_absolute() or ".." in path.parts or annotations["PATH"] != expected_path:
				self.error(custom, upstream.reference, f"PATH must be {expected_path!r}")
		if annotations.get("METHOD"):
			method = annotations["METHOD"]
			# Legacy leaf names are valid only when they identify this callable uniquely.
			valid = method == upstream.qualname
			if valid:
				try:
					active = self.resolver.function_symbol(upstream.module, method)
				except MissingSymbol:
					active = None
				except ResolutionError as exc:
					self.error(custom, upstream.reference, f"Ambiguous METHOD {method!r}: {exc}")
					return
				if (
					active is not None
					and active.module.path == upstream.module.path
					and active.qualname == upstream.qualname
					and active.node is not upstream.node
				):
					self.error(
						custom,
						upstream.reference,
						f"Ambiguous METHOD {method!r}: PATH and METHOD identify a later definition in the same file, not the inherited definition captured by this class",
					)
					return
			if not valid and method == upstream.node.name:
				try:
					valid = self.resolver.function_node(upstream.module, method) is upstream.node
				except ResolutionError as exc:
					self.error(custom, upstream.reference, str(exc))
			if not valid:
				self.error(
					custom,
					upstream.reference,
					f"METHOD must identify {upstream.qualname!r}; qualify class methods to avoid ambiguity",
				)

	def function(self, original: str, replacement: str):
		custom = self.resolver.resolve(replacement)
		try:
			if custom.module.app.name != self.app.name or not isinstance(custom.node, FUNCTION_NODES):
				raise ResolutionError("Override must resolve to a function in the custom app")
			upstream = self.resolver.resolve(original)
			if upstream.module.app.name == self.app.name or not isinstance(upstream.node, FUNCTION_NODES):
				raise ResolutionError("Original must resolve to a function in a required app or frappe")
			self.validate_annotation(custom, upstream)
		except ResolutionError as exc:
			self.error(custom, original, str(exc))

	def doctype(self, doctype: str, replacement: str, *, extension: bool | None = None):
		custom = self.resolver.resolve(replacement)
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
			# Include inherited methods from local mixins, not just the registered class.
			methods = {}
			class_bindings = {}
			for owner in hierarchy:
				bindings = scope_bindings(self.resolver.class_body(owner))
				writes = self.resolver.class_mutations(owner)
				if None in writes:
					raise ResolutionError(
						f"Ambiguous dynamic member mutation of {owner.reference} at {owner.module.path}"
					)
				for name in writes:
					bindings.setdefault(name, []).append(
						ast.copy_location(ast.Name(id=name, ctx=ast.Store()), owner.node)
					)
				class_bindings[owner.identity] = bindings
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
				resolution_error = None
				for owner in upstream_classes:
					if name not in class_bindings[owner.identity]:
						continue
					upstream_owner = owner
					try:
						value = self.resolver.member(owner, name)
					except ResolutionError as exc:
						resolution_error = exc
						value = None
					if value is not None and isinstance(value.node, FUNCTION_NODES):
						original = value
					# A descriptor or assignment shadows the same name on every ancestor.
					break
				definitions = [
					node
					for node in class_bindings[method_owner.identity][name]
					if isinstance(node, FUNCTION_NODES)
				]
				annotated = any(parse_annotation(ast.get_docstring(node) or "")[0] for node in definitions)
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
				try:
					concrete = self.resolver.member(method_owner, name)
					if concrete is not None and not isinstance(concrete.node, FUNCTION_NODES):
						concrete = None
				except ResolutionError as exc:
					resolution_error = exc
					concrete = None
				if concrete is None or (original is None and upstream_owner is not None):
					self.error(
						method,
						f"{upstream_owner.reference}.{name}" if upstream_owner else f"DocType {doctype}",
						f"Cannot resolve concrete implementation of overriding method {method.reference}"
						+ (f": {resolution_error}" if resolution_error else ""),
					)
					continue
				method = concrete
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
		mixins = [self.resolver.resolve(path) for path in reversed(paths)]
		if not any(mixin.identity == custom.identity for mixin in mixins):
			raise ResolutionError(f"Extension {custom.reference} is not registered for DocType {doctype!r}")
		if dependency_extensions:
			mixins.extend(self.resolver.resolve(path) for path in reversed(dependency_extensions[0]))
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
			controller = self.resolver.resolve(replacements[-1])
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
		hooks = read_hooks(hooks_path)
		root = apps_root.resolve() if apps_root else app.root.parent
		apps = dependency_apps(app, root, additional_dependencies)
		validator = AnnotationValidator(app, SourceResolver(apps))
		for hook in ("override_whitelisted_methods", "override_doctype_class", "extend_doctype_class"):
			for original, replacement in hook_mapping(hooks, hook):
				try:
					if hook == "override_whitelisted_methods":
						validator.function(original, replacement)
					else:
						validator.doctype(original, replacement, extension=hook == "extend_doctype_class")
				except ResolutionError as exc:
					validator.result.errors.append(
						f"{hooks_path}: {hook}[{original!r}] -> {replacement}: {exc}"
					)
		return validator.result
	except ResolutionError as exc:
		result.errors.append(str(exc))
	return result
