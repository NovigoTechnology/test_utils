"""Shared parsing of override annotations and repository identities."""

import ast
import io
import re
import tokenize
from urllib.parse import urlsplit

FIELDS = ("APP", "HASH", "REPO", "PATH", "METHOD")
TRACKING_FIELDS = ("HASH", "REPO", "PATH", "METHOD")
FIELD_PATTERN = re.compile(r"^[ \t]*(APP|HASH|REPO|PATH|METHOD):[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def parse_annotation(text: str) -> tuple[dict[str, str], list[str]]:
	fields = {}
	duplicates = []
	for match in FIELD_PATTERN.finditer(text):
		name, value = match.groups()
		if name in fields and name not in duplicates:
			duplicates.append(name)
		fields[name] = value
	return fields, duplicates


def iter_annotations(source: str):
	"""Read each string/comment block independently, without executing Python."""

	def complete(text):
		fields, duplicates = parse_annotation(text)
		return fields if not duplicates and all(fields.get(key) for key in TRACKING_FIELDS) else None

	try:
		tree = ast.parse(source)
	except SyntaxError:
		tree = None
	if tree is not None:
		for node in ast.walk(tree):
			if isinstance(node, ast.Constant) and isinstance(node.value, str):
				if fields := complete(node.value):
					yield fields
	comments = []
	last_line = 0
	try:
		for token in tokenize.generate_tokens(io.StringIO(source).readline):
			if token.type == tokenize.COMMENT:
				if comments and token.start[0] > last_line + 1:
					if fields := complete("\n".join(comments)):
						yield fields
					comments = []
				comments.append(token.string[1:].lstrip())
				last_line = token.start[0]
			elif token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}:
				if comments:
					if fields := complete("\n".join(comments)):
						yield fields
					comments = []
				if tree is None and token.type == tokenize.STRING:
					try:
						value = ast.literal_eval(token.string)
					except SyntaxError, ValueError:
						continue
					if isinstance(value, str) and (fields := complete(value)):
						yield fields
	except tokenize.TokenError, IndentationError:
		pass
	if comments and (fields := complete("\n".join(comments))):
		yield fields


def repository_identity(value: str) -> str:
	"""Normalize HTTPS, SSH and SCP remotes without user credentials or .git."""
	if re.match(r"^[\w.-]+@[\w.-]+:", value):
		value = "ssh://" + value.replace(":", "/", 1)
	parsed = urlsplit(value)
	if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
		raise ValueError("REPO must be an HTTP(S), SSH or Git repository URL")
	path = parsed.path.rstrip("/").removesuffix(".git")
	if not path or path == "/" or parsed.query or parsed.fragment:
		raise ValueError("REPO must identify a repository without a query or fragment")
	host = parsed.hostname.lower()
	if host == "github.com":
		path = path.lower()
	default_port = {"http": 80, "https": 443, "ssh": 22, "git": 9418}[parsed.scheme]
	port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
	return f"{host}{port}{path}"


def github_repository(value: str) -> tuple[str, str]:
	identity = repository_identity(value)
	parts = identity.split("/")
	if (
		len(parts) != 3
		or parts[0] != "github.com"
		or not all(re.fullmatch(r"[\w.-]+", part) for part in parts[1:])
	):
		raise ValueError("REPO must identify a github.com owner/repository supported by track_overrides")
	return parts[1], parts[2]
