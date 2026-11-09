"""Structured override analysis for external integrations."""

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from test_utils.utils.local_overrides import git_result
from test_utils.utils.override_annotations import (
	AnnotationValidator,
	ResolutionError,
	SourceResolver,
	dependency_apps,
	locate_app,
	read_tree,
	run_override_validation,
)
from test_utils.utils.override_metadata import (
	annotation_field_errors,
	github_repository,
	parse_annotation,
	repository_identity,
)
from test_utils.utils.override_tracking import compare_override
from test_utils.utils.revision_sources import RevisionSources
from test_utils.utils.track_overrides import UpstreamFetchError

SCHEMA_VERSION = 1


def package_version():
	try:
		return version("test_utils")
	except PackageNotFoundError:
		return "unknown"


def source_hash(source):
	return hashlib.sha256(source.encode()).hexdigest()


def normalized_repository(value):
	return repository_identity(value) if value else None


def override_id(repository, file_path, symbol):
	payload = json.dumps([repository, file_path, symbol], separators=(",", ":"))
	return "sha256:" + source_hash(payload)


def git_revision(app):
	result = git_result(app.root, "rev-parse", "--verify", "HEAD^{commit}")
	if result.returncode:
		raise ResolutionError(f"Cannot resolve Git HEAD in {app.root}: {result.stderr.strip()}")
	status = git_result(app.root, "status", "--porcelain", "--untracked-files=all")
	if status.returncode:
		raise ResolutionError(f"Cannot inspect Git status in {app.root}: {status.stderr.strip()}")
	if status.stdout:
		raise ResolutionError(
			f"Checkout {app.root} has uncommitted changes; analyze_overrides requires immutable revisions"
		)
	return result.stdout.strip().lower()


def annotation_diagnostic(message):
	if message.startswith("Missing annotation fields:"):
		return "annotation_missing"
	if message.startswith("Duplicate annotation field"):
		return "annotation_duplicate"
	return "annotation_invalid"


class StructuredValidator(AnnotationValidator):
	"""Expose AnnotationValidator discoveries and diagnostics as data."""

	def __init__(self, app, resolver, hooks_path):
		super().__init__(app, resolver)
		self.hooks_path = hooks_path
		self.context = {}
		self.validating_annotation = False
		self.registrations = []
		self.findings = {}

	def custom_location(self, custom):
		try:
			return custom.module.path.relative_to(self.app.root).as_posix(), custom.node.lineno
		except ValueError:
			return self.hooks_path.relative_to(self.app.root).as_posix(), 1

	def collect(self, custom_file, custom_symbol, line, upstream, diagnostic, annotation=None):
		key = (custom_file, custom_symbol, upstream)
		item = self.findings.setdefault(
			key,
			{
				"custom_file": custom_file,
				"custom_symbol": custom_symbol,
				"line": line,
				"annotation": annotation or {},
				"duplicate_fields": [],
				"custom_body_sha256": None,
				"upstream_symbols": [],
				"diagnostics": [],
				"discovery": dict(self.context),
			},
		)
		if upstream and upstream not in item["upstream_symbols"]:
			item["upstream_symbols"].append(upstream)
		if diagnostic and diagnostic not in item["diagnostics"]:
			item["diagnostics"].append(diagnostic)
		return item

	def error(self, custom, upstream, message):
		fields, duplicates = parse_annotation(ast.get_docstring(custom.node) or "")
		kind = "annotation" if self.validating_annotation else "resolution"
		code = annotation_diagnostic(message) if self.validating_annotation else "upstream_unresolved"
		custom_file, line = self.custom_location(custom)
		item = self.collect(
			custom_file,
			custom.reference,
			line,
			upstream,
			{"code": code, "kind": kind, "message": message},
			fields,
		)
		item["duplicate_fields"] = duplicates
		try:
			source = custom.module.path.read_text(encoding="utf-8-sig")
			body = ast.get_source_segment(source, custom.node) or ""
			item["custom_body_sha256"] = source_hash(body)
		except OSError:
			pass
		super().error(custom, upstream, message)

	def validate_annotation(self, custom, upstream):
		key = (custom.identity, upstream.identity)
		if key in self.checked:
			return
		fields, duplicates = parse_annotation(ast.get_docstring(custom.node) or "")
		custom_file, line = self.custom_location(custom)
		source = custom.module.path.read_text(encoding="utf-8-sig")
		body = ast.get_source_segment(source, custom.node) or ""
		self.registrations.append(
			{
				"custom_file": custom_file,
				"custom_symbol": custom.reference,
				"line": line,
				"annotation": fields,
				"duplicate_fields": duplicates,
				"custom_body_sha256": source_hash(body),
				"upstream_app": upstream.module.app.name,
				"upstream_repository": normalized_repository(upstream.module.app.repository),
				"upstream_file": upstream.module.path.relative_to(upstream.module.app.root).as_posix(),
				"upstream_symbol": upstream.reference,
				"discovery": dict(self.context),
			}
		)
		self.collect(custom_file, custom.reference, line, upstream.reference, None, fields)
		self.validating_annotation = True
		try:
			super().validate_annotation(custom, upstream)
		finally:
			self.validating_annotation = False


def registration_line(hooks_path, replacement):
	return next(
		(
			node.lineno
			for node in ast.walk(read_tree(hooks_path))
			if isinstance(node, ast.Constant) and node.value == replacement
		),
		1,
	)


def validate_registered_overrides(app, apps):
	hooks_path = app.package / "hooks.py"
	validator = StructuredValidator(app, SourceResolver(apps), hooks_path)

	def before_registration(hook, original, replacement):
		validator.context = {
			"kind": "hook",
			"hook": hook,
			"file": hooks_path.relative_to(app.root).as_posix(),
			"line": registration_line(hooks_path, replacement),
			"original_reference": original,
			"replacement_reference": replacement,
		}

	def resolution_error(hook, original, replacement, exc):
		validator.collect(
			validator.context["file"],
			replacement,
			validator.context["line"],
			original,
			{"code": "registration_unresolved", "kind": "resolution", "message": str(exc)},
		)

	run_override_validation(
		app,
		apps,
		validator,
		before_registration=before_registration,
		resolution_error=resolution_error,
	)
	return {
		"checked": validator.result.checked,
		"errors": validator.result.errors,
		"registrations": validator.registrations,
		"findings": list(validator.findings.values()),
	}


def resolved_revisions(apps, requested):
	result = {}
	for app in apps:
		head = git_revision(app)
		if app.name in requested and requested[app.name].lower() != head:
			raise ResolutionError(
				f"Requested revision for {app.name} is {requested[app.name]}, but checkout HEAD is {head}"
			)
		result[app.name] = head
	unknown = set(requested) - set(result)
	if unknown:
		raise ResolutionError(f"Revisions were provided for unavailable apps: {', '.join(sorted(unknown))}")
	return result


def source_context(apps):
	repositories = {}
	for app in apps:
		repository = normalized_repository(app.repository)
		if repository:
			if repository in repositories:
				raise ResolutionError(f"Multiple app checkouts identify repository {repository}")
			repositories[repository] = app
	return repositories


def diagnostics_for(findings, symbol, upstream):
	return [
		diagnostic
		for item in findings
		if item["custom_symbol"] == symbol and upstream in item["upstream_symbols"]
		for diagnostic in item["diagnostics"]
	]


def analyze_registered_override(record, app, repositories, revisions, findings):
	owner_repository = normalized_repository(app.repository) or app.name
	diagnostics = diagnostics_for(findings, record["custom_symbol"], record["upstream_symbol"])
	fields = record["annotation"]
	result = {
		"override_id": override_id(owner_repository, record["custom_file"], record["custom_symbol"]),
		"classification": "invalid_annotation",
		"discovery": record["discovery"],
		"owner": {
			"app": app.name,
			"repository": owner_repository,
			"file": record["custom_file"],
			"line": record["line"],
			"symbol": record["custom_symbol"],
			"revision": revisions[app.name],
			"body_sha256": record["custom_body_sha256"],
		},
		"upstream": {
			"app": record["upstream_app"],
			"repository": record["upstream_repository"],
			"file": record["upstream_file"],
			"symbol": record["upstream_symbol"],
			"baseline_revision": fields.get("HASH", "").lower() or None,
			"target_revision": revisions[record["upstream_app"]],
			"baseline_body_sha256": None,
			"target_body_sha256": None,
		},
		"annotation": {
			"fields": fields,
			"duplicate_fields": record["duplicate_fields"],
			"repository_identity": None,
		},
		"change_kind": None,
		"diff": "",
		"diagnostics": diagnostics,
	}
	try:
		annotation_errors = [item for item in diagnostics if item["kind"] == "annotation"]
		if annotation_errors:
			if record["annotation"].get("REPO"):
				try:
					result["annotation"]["repository_identity"] = repository_identity(
						record["annotation"]["REPO"]
					)
				except ValueError:
					pass
			return result
		repository = repository_identity(fields["REPO"])
		result["annotation"]["repository_identity"] = repository
		if repository not in repositories:
			raise ValueError(f"REPO {record['annotation']['REPO']!r} does not match a dependency checkout")
		upstream = repositories[repository]
		if upstream.name != record["upstream_app"]:
			raise ValueError("REPO does not identify the application that owns the resolved upstream symbol")
		baseline = RevisionSources(fields["REPO"], fields["HASH"], checkout=upstream.root)
		target = RevisionSources(fields["REPO"], revisions[upstream.name], checkout=upstream.root)
		comparison = compare_override(baseline, target, fields["PATH"], fields["METHOD"])
		result["upstream"]["baseline_body_sha256"] = comparison.baseline_body_sha256
		result["upstream"]["target_body_sha256"] = comparison.target_body_sha256
		result["change_kind"] = comparison.change_kind
		result["diff"] = comparison.diff
		if comparison.status == "changed":
			result["classification"] = "affected"
		elif comparison.status == "unverified":
			result["classification"] = (
				"technical_error" if comparison.error_kind == "source_access" else "inconclusive"
			)
			result["diagnostics"].append(
				{
					"code": "source_access_error"
					if comparison.error_kind == "source_access"
					else "comparison_inconclusive",
					"kind": comparison.error_kind or "resolution",
					"message": comparison.diagnostic,
				}
			)
		elif any(item["kind"] == "resolution" for item in diagnostics):
			result["classification"] = "inconclusive"
		else:
			result["classification"] = "unaffected"
	except ValueError as exc:
		result["diagnostics"].append(
			{"code": "annotation_invalid", "kind": "annotation", "message": str(exc)}
		)
	except (ResolutionError, UpstreamFetchError, OSError) as exc:
		result["classification"] = "technical_error"
		result["diagnostics"].append(
			{"code": "analysis_error", "kind": "source_access", "message": str(exc)}
		)
	return result


def unresolved_result(item, app, repositories, revisions):
	repository = normalized_repository(app.repository) or app.name
	annotation_errors = any(diagnostic["kind"] == "annotation" for diagnostic in item["diagnostics"])
	result = {
		"override_id": override_id(repository, item["custom_file"], item["custom_symbol"]),
		"classification": "invalid_annotation" if annotation_errors else "inconclusive",
		"discovery": item["discovery"],
		"owner": {
			"app": app.name,
			"repository": repository,
			"file": item["custom_file"],
			"line": item["line"],
			"symbol": item["custom_symbol"],
			"revision": revisions[app.name],
			"body_sha256": item["custom_body_sha256"],
		},
		"upstream": {
			"app": None,
			"repository": None,
			"file": None,
			"symbol": item["upstream_symbols"][0] if item["upstream_symbols"] else None,
			"baseline_revision": item["annotation"].get("HASH") or None,
			"target_revision": None,
			"baseline_body_sha256": None,
			"target_body_sha256": None,
		},
		"annotation": {
			"fields": item["annotation"],
			"duplicate_fields": item["duplicate_fields"],
			"repository_identity": None,
		},
		"change_kind": None,
		"diff": "",
		"diagnostics": item["diagnostics"],
	}
	if annotation_errors:
		return result
	field_errors = annotation_field_errors(
		item["annotation"], item["duplicate_fields"], app.name
	)
	if field_errors:
		if item["custom_body_sha256"] is None and not item["annotation"]:
			return result
		result["classification"] = "invalid_annotation"
		for _, message in field_errors:
			result["diagnostics"].append(
				{
					"code": annotation_diagnostic(message),
					"kind": "annotation",
					"message": message,
				}
			)
		return result
	try:
		upstream_repository = repository_identity(item["annotation"]["REPO"])
		github_repository(item["annotation"]["REPO"])
		result["annotation"]["repository_identity"] = upstream_repository
		if upstream_repository not in repositories:
			raise ValueError(
				f"REPO {item['annotation']['REPO']!r} does not match a dependency checkout"
			)
		upstream = repositories[upstream_repository]
		fields = item["annotation"]
		result["upstream"].update(
			{
				"app": upstream.name,
				"repository": upstream_repository,
				"file": fields["PATH"],
				"baseline_revision": fields["HASH"].lower(),
				"target_revision": revisions[upstream.name],
			}
		)
		baseline = RevisionSources(fields["REPO"], fields["HASH"], checkout=upstream.root)
		target = RevisionSources(fields["REPO"], revisions[upstream.name], checkout=upstream.root)
		comparison = compare_override(baseline, target, fields["PATH"], fields["METHOD"])
		result["upstream"]["baseline_body_sha256"] = comparison.baseline_body_sha256
		result["upstream"]["target_body_sha256"] = comparison.target_body_sha256
		result["change_kind"] = comparison.change_kind
		result["diff"] = comparison.diff
		if comparison.status == "changed" and comparison.change_kind == "missing":
			result["classification"] = "affected"
		elif comparison.status == "unverified":
			result["classification"] = (
				"technical_error" if comparison.error_kind == "source_access" else "inconclusive"
			)
			result["diagnostics"].append(
				{
					"code": "source_access_error"
					if comparison.error_kind == "source_access"
					else "comparison_inconclusive",
					"kind": comparison.error_kind or "resolution",
					"message": comparison.diagnostic,
				}
			)
	except ValueError as exc:
		result["diagnostics"].append(
			{"code": "annotation_invalid", "kind": "annotation", "message": str(exc)}
		)
	except (ResolutionError, UpstreamFetchError, OSError) as exc:
		result["classification"] = "technical_error"
		result["diagnostics"].append(
			{"code": "analysis_error", "kind": "source_access", "message": str(exc)}
		)
	return result


def outcome(results, errors):
	classifications = {item["classification"] for item in results}
	if errors or classifications & {"inconclusive", "technical_error"}:
		return "incomplete"
	if classifications & {"affected", "invalid_annotation"}:
		return "completed_with_findings"
	return "completed"


def analyze_overrides(app_path, apps_root, additional_dependencies=(), revisions=None):
	"""Analyze one app using sibling checkouts and return the versioned contract."""
	revisions = revisions or {}
	try:
		app = locate_app(Path(app_path))
		apps = dependency_apps(app, Path(apps_root).resolve(), additional_dependencies)
		resolved = resolved_revisions(apps, revisions)
		repositories = source_context(apps)
		validation = validate_registered_overrides(app, apps)
		results = [
			analyze_registered_override(
				record,
				app,
				repositories,
				resolved,
				validation["findings"],
			)
			for record in validation["registrations"]
		]
		registered_pairs = {
			(item["custom_symbol"], item["upstream_symbol"])
			for item in validation["registrations"]
		}
		results.extend(
			unresolved_result(item, app, repositories, resolved)
			for item in validation["findings"]
			if not any(
				(item["custom_symbol"], upstream) in registered_pairs
				for upstream in item["upstream_symbols"]
			)
		)
		errors = []
		subject_repository = normalized_repository(app.repository)
		document = {
			"schema_version": SCHEMA_VERSION,
			"tool": {"name": "test_utils", "version": package_version()},
			"subject": {
				"app": app.name,
				"repository": subject_repository,
				"revision": resolved[app.name],
			},
			"sources": [
				{
					"app": source.name,
					"repository": normalized_repository(source.repository),
					"revision": resolved[source.name],
				}
				for source in apps
				if source.name != app.name
			],
			"results": results,
			"errors": errors,
		}
	except (ResolutionError, UpstreamFetchError, ValueError, OSError) as exc:
		document = {
			"schema_version": SCHEMA_VERSION,
			"tool": {"name": "test_utils", "version": package_version()},
			"subject": {"app": Path(app_path).name, "repository": None, "revision": None},
			"sources": [],
			"results": [],
			"errors": [{"code": "analysis_setup_error", "kind": "technical", "message": str(exc)}],
		}
	document["counts"] = dict(Counter(item["classification"] for item in document["results"]))
	document["outcome"] = outcome(document["results"], document["errors"])
	return document


def parse_revision(value):
	name, separator, revision = value.partition("=")
	if not separator or not name.isidentifier() or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
		raise argparse.ArgumentTypeError("revision must use APP=40_CHARACTER_COMMIT")
	return name, revision.lower()


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--app", type=Path, required=True)
	parser.add_argument("--apps-root", type=Path, required=True)
	parser.add_argument("--dependency", action="append", default=[])
	parser.add_argument("--revision", action="append", type=parse_revision, default=[])
	parser.add_argument("--output", default="-", help="JSON output file, or - for stdout")
	args = parser.parse_args(argv)
	document = analyze_overrides(
		args.app,
		args.apps_root,
		args.dependency,
		dict(args.revision),
	)
	payload = json.dumps(document, indent=2) + "\n"
	if args.output == "-":
		sys.stdout.write(payload)
	else:
		Path(args.output).write_text(payload, encoding="utf-8")
	return 2 if document["outcome"] == "incomplete" else 0


if __name__ == "__main__":
	raise SystemExit(main())
