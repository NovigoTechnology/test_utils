"""Discover Frappe overrides and validate upstream annotations using local ASTs."""

import ast
import json
import re
import subprocess
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from test_utils.utils.python_source import FUNCTION_NODES, find_definition, scope_statements

HOOKS = {"required_apps", "override_whitelisted_methods", "override_doctype_class", "extend_doctype_class"}
FIELDS = ("APP", "HASH", "REPO", "PATH", "METHOD")


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


def literal(node: ast.expr, values: dict):
	"""Evaluate only data expressions, never calls to application code."""
	if isinstance(node, ast.Name) and node.id in values:
		return values[node.id]
	if isinstance(node, ast.Constant):
		return node.value
	if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
		return [literal(item, values) for item in node.elts]
	if isinstance(node, ast.Dict):
		result = {}
		for key, value in zip(node.keys, node.values, strict=True):
			if key is None:
				result.update(literal(value, values))
			else:
				result[literal(key, values)] = literal(value, values)
		return result
	if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
		left, right = literal(node.left, values), literal(node.right, values)
		return left | right if isinstance(node.op, ast.BitOr) else left + right
	raise ResolutionError(f"Unsupported dynamic hook expression at line {node.lineno}; use static values")


def read_hooks(path: Path) -> dict:
	values = {}
	for node in read_tree(path).body:
		if isinstance(node, (ast.Assign, ast.AnnAssign)):
			targets = node.targets if isinstance(node, ast.Assign) else [node.target]
			names = [target.id for target in targets if isinstance(target, ast.Name)]
			if any(
				isinstance(target, ast.Subscript)
				and any(isinstance(child, ast.Name) and child.id in HOOKS for child in ast.walk(target))
				for target in targets
			):
				raise ResolutionError(
					f"{path}:{node.lineno}: hook item assignment is unsupported; use a static dictionary"
				)
			try:
				value = literal(node.value, values)
			except ResolutionError, TypeError, ValueError, AttributeError:
				if HOOKS.intersection(names):
					raise ResolutionError(f"{path}:{node.lineno}: hook {names} must use static values")
				for name in names:
					values.pop(name, None)
				continue
			values.update(dict.fromkeys(names, value))
		elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
			name = node.target.id
			try:
				value = literal(
					ast.copy_location(ast.BinOp(left=node.target, op=node.op, right=node.value), node), values
				)
				# += on lists and |= on dictionaries also update aliases in Python.
				if isinstance(values.get(name), list) and isinstance(value, list):
					values[name][:] = value
				elif isinstance(values.get(name), dict) and isinstance(value, dict):
					values[name].update(value)
				else:
					values[name] = value
			except (ResolutionError, TypeError, KeyError) as exc:
				values.pop(name, None)
				if name in HOOKS:
					raise ResolutionError(f"{path}:{node.lineno}: cannot statically evaluate {name}") from exc
		else:
			# Conditional assignments and mutations must not silently hide registrations.
			for child in ast.walk(node):
				if isinstance(child, ast.Name) and child.id in HOOKS:
					raise ResolutionError(
						f"{path}:{child.lineno}: dynamic configuration of {child.id}; use top-level static assignments"
					)
	return {name: values[name] for name in HOOKS if name in values}


def normalize_required_app(value: str) -> tuple[str, str | None]:
	if not isinstance(value, str) or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)?", value):
		raise ResolutionError(f"Invalid required_apps entry: {value!r}; expected app or owner/repository")
	name = value.rsplit("/", 1)[-1].removesuffix(".git").replace("-", "_")
	if not name.isidentifier():
		raise ResolutionError(f"Invalid app name in required_apps: {value!r}")
	return name, f"https://github.com/{value.removesuffix('.git')}" if "/" in value else None


def repository_identity(value: str) -> str:
	"""Compare HTTPS, SSH and SCP Git remotes without credentials or .git suffixes."""
	if re.match(r"^[\w.-]+@[\w.-]+:", value):
		value = "ssh://" + value.replace(":", "/", 1)
	parsed = urlsplit(value)
	if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
		raise ResolutionError("REPO must be an HTTP(S), SSH or Git repository URL")
	path = parsed.path.rstrip("/").removesuffix(".git")
	if not path or path == "/" or parsed.query or parsed.fragment:
		raise ResolutionError("REPO must identify a repository without a query or fragment")
	host = parsed.hostname.lower()
	if host == "github.com":
		path = path.lower()
	default_port = {"http": 80, "https": 443, "ssh": 22, "git": 9418}[parsed.scheme]
	port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
	return f"{host}{port}{path}"


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
	node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef

	@property
	def reference(self) -> str:
		return f"{self.module.name}.{self.qualname}"


class SourceResolver:
	def __init__(self, apps: list[App]):
		self.apps = {app.name: app for app in apps}
		self.modules: dict[Path, Module] = {}

	def module(self, app: App, name: str, path: Path) -> Module:
		if path not in self.modules:
			self.modules[path] = Module(app, name, path, read_tree(path))
		return self.modules[path]

	def resolve(self, reference: str, seen: frozenset[str] = frozenset()) -> Symbol:
		if reference in seen:
			raise ResolutionError(f"Cyclic import while resolving {reference}")
		seen = seen | {reference}
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
				node = find_definition(module.tree, ".".join(remaining))
				if node is not None:
					return Symbol(module, ".".join(remaining), node)
				if remaining[0] in module.imports:
					return self.resolve(".".join([module.imports[remaining[0]], *remaining[1:]]), seen)
				raise ResolutionError(
					f"Upstream symbol {reference} not found in {path}; it may have been removed or renamed"
				)
		raise ResolutionError(
			f"Cannot resolve {reference}: source file not found; check --apps-root and upstream renames"
		)

	def base_classes(self, symbol: Symbol) -> list[Symbol]:
		result = []
		for base in symbol.node.bases:
			name = dotted_name(base)
			if name in {"object", "typing.Protocol", "Protocol", "ABC", "abc.ABC"}:
				continue
			first, *rest = name.split(".")
			reference = ".".join([symbol.module.imports.get(first, f"{symbol.module.name}.{first}"), *rest])
			resolved = self.resolve(reference)
			if not isinstance(resolved.node, ast.ClassDef):
				raise ResolutionError(f"Base {reference} is not a class")
			result.append(resolved)
		return result

	def hierarchy(self, symbol: Symbol, seen: frozenset[str] = frozenset()) -> list[Symbol]:
		"""Python C3 linearization, so multiple inheritance resolves the real owner."""
		if symbol.reference in seen:
			raise ResolutionError(f"Cyclic inheritance at {symbol.reference}")
		bases = self.base_classes(symbol)
		sequences = [self.hierarchy(base, seen | {symbol.reference}) for base in bases] + [bases[:]]
		result = [symbol]
		while any(sequences):
			sequences = [seq for seq in sequences if seq]
			candidate = next(
				(
					seq[0]
					for seq in sequences
					if all(seq[0].reference not in [s.reference for s in other[1:]] for other in sequences)
				),
				None,
			)
			if candidate is None:
				raise ResolutionError(f"Inconsistent inheritance at {symbol.reference}")
			result.append(candidate)
			for seq in sequences:
				if seq[0].reference == candidate.reference:
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
				node = find_definition(module.tree, class_name)
				if not isinstance(node, ast.ClassDef):
					raise ResolutionError(
						f"Controller {module_name}.{class_name} not found; it may have been renamed"
					)
				found.append(Symbol(module, class_name, node))
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
		self.checked: set[tuple[str, str]] = set()

	def error(self, custom: Symbol, upstream: str, message: str):
		self.result.errors.append(
			f"{custom.module.path}:{custom.node.lineno}: {custom.reference} -> {upstream}: {message}"
		)

	def validate_annotation(self, custom: Symbol, upstream: Symbol):
		key = (custom.reference, upstream.reference)
		if key in self.checked:
			return
		self.checked.add(key)
		self.result.checked += 1
		annotations = {}
		for line in (ast.get_docstring(custom.node) or "").splitlines():
			match = re.fullmatch(r"\s*(APP|HASH|REPO|PATH|METHOD):\s*(.*?)\s*", line)
			if match:
				if match[1] in annotations:
					self.error(custom, upstream.reference, f"Duplicate annotation field {match[1]}")
				annotations[match[1]] = match[2]
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
				if repository_identity(annotations["REPO"]) != repository_identity(expected):
					raise ResolutionError(f"REPO does not match the upstream checkout ({expected})")
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
			if not valid and method == upstream.node.name:
				matches = [
					n
					for n in ast.walk(upstream.module.tree)
					if isinstance(n, FUNCTION_NODES) and n.name == method
				]
				valid = len(matches) == 1
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

	def doctype(self, doctype: str, replacement: str):
		custom = self.resolver.resolve(replacement)
		try:
			if custom.module.app.name != self.app.name or not isinstance(custom.node, ast.ClassDef):
				raise ResolutionError("DocType override must resolve to a class in the custom app")
			hierarchy = self.resolver.hierarchy(custom)
			controller = self.resolver.controller(doctype, self.app.name)
			if controller:
				known = {s.reference for s in hierarchy}
				hierarchy.extend(s for s in self.resolver.hierarchy(controller) if s.reference not in known)
			upstream_classes = [s for s in hierarchy if s.module.app.name != self.app.name]
			if not upstream_classes:
				raise ResolutionError(
					f"Cannot resolve upstream controller or base class for DocType {doctype!r}"
				)
			# Include inherited methods from local mixins, not just the registered class.
			methods = {}
			for owner in hierarchy:
				for node in scope_statements(owner.node.body):
					if isinstance(node, FUNCTION_NODES):
						methods.setdefault(
							node.name, Symbol(owner.module, f"{owner.qualname}.{node.name}", node)
						)
			for name, method in methods.items():
				if method.module.app.name != self.app.name:
					continue
				original = None
				for owner in upstream_classes:
					node = find_definition(owner.node, name)
					if isinstance(node, FUNCTION_NODES):
						original = Symbol(owner.module, f"{owner.qualname}.{name}", node)
						break
				if original:
					self.validate_annotation(method, original)
				elif any(
					re.match(r"\s*(?:APP|HASH|REPO|PATH|METHOD):", line)
					for line in (ast.get_docstring(method.node) or "").splitlines()
				):
					self.error(
						method,
						f"{upstream_classes[0].reference}.{name}",
						"Annotated upstream method no longer exists in the class hierarchy; it may have been removed or renamed",
					)
		except ResolutionError as exc:
			self.error(custom, f"DocType {doctype}", str(exc))


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
		if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
			raise ResolutionError(f"{name}[{key!r}] must contain dotted Python paths")
		result.extend((key, value) for value in values)
	return result


def validate_overrides(app_path: Path, apps_root: Path | None = None) -> ValidationResult:
	"""Validate a repository/package using sibling checkouts; no network or imports."""
	result = ValidationResult()
	try:
		app = locate_app(app_path)
		hooks_path = app.package / "hooks.py"
		if not hooks_path.is_file():
			raise ResolutionError(f"hooks.py not found in {app.package}")
		hooks = read_hooks(hooks_path)
		required = hooks.get("required_apps", [])
		if isinstance(required, str):
			required = [required]
		if not isinstance(required, list):
			raise ResolutionError("required_apps must be a string or a list of app names")
		dependencies = {"frappe": None}
		dependencies.update(normalize_required_app(value) for value in required)
		root = apps_root.resolve() if apps_root else app.root.parent
		apps = [app]
		for name, repository in dependencies.items():
			if name != app.name:
				apps.append(locate_app(root / name, name, repository))
		validator = AnnotationValidator(app, SourceResolver(apps))
		for hook in ("override_whitelisted_methods", "override_doctype_class", "extend_doctype_class"):
			for original, replacement in hook_mapping(hooks, hook):
				try:
					if hook == "override_whitelisted_methods":
						validator.function(original, replacement)
					else:
						validator.doctype(original, replacement)
				except ResolutionError as exc:
					validator.result.errors.append(
						f"{hooks_path}: {hook}[{original!r}] -> {replacement}: {exc}"
					)
		return validator.result
	except ResolutionError as exc:
		result.errors.append(str(exc))
	return result
