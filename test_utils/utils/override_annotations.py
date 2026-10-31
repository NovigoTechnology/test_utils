"""Discover Frappe overrides and validate upstream annotations using local ASTs."""

import ast
import json
import re
import subprocess
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath

from test_utils.utils.override_metadata import (
	FIELDS,
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


class SourceResolver:
	def __init__(self, apps: list[App]):
		self.apps = {app.name: app for app in apps}
		self.modules: dict[Path, Module] = {}

	def module(self, app: App, name: str, path: Path) -> Module:
		if path not in self.modules:
			self.modules[path] = Module(app, name, path, read_tree(path))
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
				if not path.is_file():
					continue
				return self.module(app, ".".join(parts[:end]), path), parts[end:]
			if stem.is_dir():
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
		if any(
			isinstance(node, ast.Call)
			and isinstance(node.func, ast.Name)
			and node.func.id in {"exec", "globals", "locals"}
			for statement in body
			for node in executable_nodes(statement)
		):
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
					for node in executable_nodes(statement)
				):
					raise ValueError
			except (AttributeError, ValueError, TypeError, SyntaxError) as exc:
				raise ResolutionError(f"Ambiguous dynamic __all__ in {module.path}") from exc
			return name in values
		if "__getattr__" in bindings:
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
		return self.resolve_in(module, parts, (), frozenset())

	def resolve_in(self, module, parts, limits, seen):
		body = self.module_body(module, limits)
		key = (module.path, ".".join(parts), len(body), "resolve")
		if key in seen:
			raise ResolutionError(f"Ambiguous cyclic import for {module.name}.{'.'.join(parts)}")
		seen = seen | {key}
		decorators = overload_names(body)
		for index, name in enumerate(parts):
			binding = None
			# An explicit package binding can name a class; otherwise a dotted
			# child-module path is resolved without inspecting unrelated exports.
			if (
				index == 0
				and len(parts) > 1
				and module.path.name == "__init__.py"
				and name not in scope_bindings(body)
			):
				prefix = f"{module.name}.{name}"
				stem = module.app.package.joinpath(*prefix.split(".")[1:])
				if stem.is_dir() or stem.with_suffix(".py").is_file():
					try:
						binding = self.binding(module, body, name, limits, seen, decorators)
					except ResolutionError:
						# Unresolved wildcard exports do not block an explicit child-module path.
						pass
					if binding is None:
						child, _ = self.source_module(prefix)
						return self.resolve_in(child, parts[1:], limits, seen)
			if binding is None:
				binding = self.binding(module, body, name, limits, seen, decorators)
			if binding is None:
				if any(path == module.path for path, _ in limits):
					raise ResolutionError(
						f"Ambiguous circular import: {name!r} is not yet declared in {module.path}"
					)
				raise ResolutionError(
					f"Upstream symbol {module.name}.{'.'.join(parts)} not found in {module.path}; it may have been removed or renamed"
				)
			if isinstance(binding.node, ast.alias) and index == 0:
				if isinstance(binding.statement, ast.ImportFrom):
					reference = module.import_reference(binding.statement)
					tail = parts if binding.node.name == "*" else [binding.node.name, *parts[1:]]
				else:
					reference = module.import_reference(binding.statement, binding.node)
					tail = parts[1:]
					prefix = binding.node.name.split(".")
					if not binding.node.asname and parts[: len(prefix)] == prefix:
						reference = binding.node.name
						tail = parts[len(prefix) :]
				try:
					imported, _ = self.source_module(reference)
					return self.resolve_in(
						imported,
						tail,
						(*limits, (module.path, (binding.statement.lineno, binding.statement.col_offset))),
						seen,
					)
				except ResolutionError as exc:
					raise ResolutionError(
						f"Ambiguous import at {module.path}:{binding.statement.lineno} ({'.'.join([reference, *tail])}): {exc}"
					) from exc
			if not isinstance(binding.node, DEFINITION_NODES):
				raise ResolutionError(
					f"Ambiguous non-definition {name!r} in {module.path}:{binding.statement.lineno}"
				)
			node = binding.node
			if index == len(parts) - 1:
				return Symbol(module, ".".join(parts), node, limits)
			if not isinstance(node, ast.ClassDef):
				break
			body = runtime_body(module.tree, node)
			decorators |= overload_names(body)
		raise ResolutionError(f"Cannot resolve definition {module.name}.{'.'.join(parts)}")

	def base_classes(self, symbol: Symbol) -> list[Symbol]:
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
				continue
			resolved = self.resolve_in(symbol.module, name.split("."), limits, frozenset())
			if not isinstance(resolved.node, ast.ClassDef):
				raise ResolutionError(f"Base {reference} is not a class")
			result.append(resolved)
		return result

	def method(self, owner: Symbol, name: str):
		"""Inspect the selected class body, retaining module-level overload aliases."""
		try:
			binding = select_binding(
				runtime_body(owner.module.tree, owner.node),
				name,
				decorators=overload_names(owner.module.tree.body),
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
				symbol = self.resolve(f"{module_name}.{class_name}")
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
			class_bindings = {
				owner.identity: scope_bindings(runtime_body(owner.module.tree, owner.node))
				for owner in hierarchy
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
