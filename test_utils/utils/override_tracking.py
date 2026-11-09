"""Compare statically resolved implementations from two repository revisions."""

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import requests

from test_utils.utils.override_annotations import (
	App,
	MissingSymbolError,
	ResolutionError,
	SourceResolver,
	Symbol,
)
from test_utils.utils.python_source import FUNCTION_NODES, runtime_body, scope_bindings, scope_statements
from test_utils.utils.track_overrides import UpstreamFetchError, compare_method_diff


class RevisionResolver(SourceResolver):
	"""Use the annotation resolver with files exclusively from one Git revision."""

	root = Path("/__override_sources__")

	def __init__(self, sources, file_path):
		path = PurePosixPath(file_path)
		parts = list(path.with_suffix("").parts)
		if parts and parts[-1] == "__init__":
			parts.pop()
		if path.suffix != ".py" or not parts or not all(part.isidentifier() for part in parts):
			raise ResolutionError(f"PATH must identify a Python module: {file_path!r}")
		self.sources = sources
		self.reference = ".".join(parts)
		self.file_path = file_path
		roots = {parts[0]}
		for source_path in self.sources.paths:
			provider = PurePosixPath(source_path)
			if provider.suffix != ".py":
				continue
			name = provider.parts[0] if len(provider.parts) > 1 else provider.stem
			if name.isidentifier():
				roots.add(name)
		super().__init__(
			[App(name, self.root, self.root / name) for name in sorted(roots)],
			require_unique_definitions=True,
		)

	def relative(self, path):
		return path.relative_to(self.root).as_posix()

	def is_file(self, path):
		return self.sources.is_file(self.relative(path))

	def is_dir(self, path):
		return self.sources.is_dir(self.relative(path))

	def source_module(self, reference, *, require_symbol=False):
		if reference == self.reference and not require_symbol:
			app = self.apps[reference.split(".")[0]]
			return self.module(app, reference, self.root / self.file_path), []
		return super().source_module(reference, require_symbol=require_symbol)

	def read_tree(self, path):
		source = self.sources.read(self.relative(path))
		if source is None:
			raise ResolutionError(f"Source file not found: {self.relative(path)}")
		try:
			return ast.parse(source.lstrip("\ufeff"), filename=self.relative(path))
		except (SyntaxError, ValueError) as exc:
			raise ResolutionError(f"Cannot parse {self.relative(path)}: {exc}") from exc

	def method_symbol(self, method):
		if not self.sources.is_file(self.file_path):
			raise MissingSymbolError(f"Source file {self.file_path} is absent")
		try:
			symbol = self.resolve_from(self.reference, method)
		except MissingSymbolError:
			if "." in method:
				raise
			# Preserve unique legacy bare method names. Each candidate still goes
			# through the shared resolver, including wildcard and binding checks.
			module, _ = self.source_module(self.reference)
			candidates = set()

			def collect(owner, prefix=""):
				for node in scope_statements(runtime_body(module.tree, owner)):
					if isinstance(node, FUNCTION_NODES) and node.name == method:
						candidates.add(prefix + method)
					elif isinstance(node, ast.ClassDef):
						collect(node, prefix + node.name + ".")

			collect(module.tree)
			if not candidates:
				raise
			if len(candidates) != 1:
				raise ResolutionError(f"Ambiguous legacy METHOD {method!r}; use Class.method") from None
			symbol = self.resolve_from(self.reference, next(iter(candidates)))
		if not isinstance(symbol.node, FUNCTION_NODES):
			raise ResolutionError(f"METHOD {method!r} does not resolve to a function")
		return symbol

	def registered_method_symbol(self, reference):
		"""Resolve a registered class method through the class's effective hierarchy."""
		owner_reference, separator, name = reference.rpartition(".")
		if not separator:
			raise ResolutionError(f"Invalid registered method reference: {reference!r}")
		owner = self.resolve_registration(owner_reference)
		if not isinstance(owner.node, ast.ClassDef):
			raise ResolutionError(f"Registered method owner {owner_reference!r} is not a class")
		for candidate in self.hierarchy(owner):
			bindings = scope_bindings(runtime_body(candidate.module.tree, candidate.node))
			if name not in bindings:
				continue
			node = self.method(candidate, name)
			if node is None:
				raise ResolutionError(
					f"Unsupported binding shadows registered method {candidate.reference}.{name}"
				)
			return Symbol(candidate.module, f"{candidate.qualname}.{name}", node)
		raise MissingSymbolError(f"Registered method {reference} is absent from the resolved hierarchy")

	def implementation(self, method):
		symbol = self.method_symbol(method)
		source = self.sources.read(self.relative(symbol.module.path))
		start = min([symbol.node.lineno, *(dec.lineno for dec in symbol.node.decorator_list)])
		return "\n".join(source.splitlines()[start - 1 : symbol.node.end_lineno])


@dataclass(frozen=True)
class Comparison:
	status: Literal["changed", "unchanged", "unverified"]
	diff: str = ""
	diagnostic: str = ""
	error_kind: Literal["resolution", "source_access"] | None = None
	baseline_body_sha256: str | None = None
	target_body_sha256: str | None = None
	change_kind: Literal["modified", "missing"] | None = None


def compare_override(before, after, file_path, method):
	"""Only a resolvable baseline permits a confirmed change or deletion."""
	try:
		old = RevisionResolver(before, file_path).implementation(method)
	except ResolutionError as exc:
		return Comparison(
			"unverified", diagnostic=f"Baseline {before.revision}: {exc}", error_kind="resolution"
		)
	except (UpstreamFetchError, requests.RequestException, OSError) as exc:
		return Comparison(
			"unverified", diagnostic=f"Baseline {before.revision}: {exc}", error_kind="source_access"
		)
	try:
		new = RevisionResolver(after, file_path).implementation(method)
	except MissingSymbolError:
		new = None
	except ResolutionError as exc:
		return Comparison("unverified", diagnostic=f"Target {after.revision}: {exc}", error_kind="resolution")
	except (UpstreamFetchError, requests.RequestException, OSError) as exc:
		return Comparison(
			"unverified", diagnostic=f"Target {after.revision}: {exc}", error_kind="source_access"
		)
	diff = compare_method_diff(old, new)
	return Comparison(
		"changed" if diff else "unchanged",
		diff=diff,
		baseline_body_sha256=hashlib.sha256(old.encode()).hexdigest(),
		target_body_sha256=hashlib.sha256(new.encode()).hexdigest() if new is not None else None,
		change_kind="missing" if new is None else "modified" if diff else None,
	)
