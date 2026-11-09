import json
import subprocess
from pathlib import Path

import pytest

from test_utils.utils import override_analysis
from test_utils.utils.override_analysis import analyze_overrides, main
from test_utils.utils.track_overrides import UpstreamFetchError


SCHEMA = Path(__file__).resolve().parents[2] / "test_utils/schemas/override_analysis_v1.schema.json"


def git(root, *args):
	return subprocess.run(
		["git", "-C", str(root), *args], check=True, capture_output=True, text=True
	).stdout.strip()


def write(path, source):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(source, encoding="utf-8")
	return path


def repository(root, name):
	root.mkdir()
	git(root, "init", "-q")
	git(root, "config", "user.name", "Override Analysis Tests")
	git(root, "config", "user.email", "overrides@example.invalid")
	git(root, "remote", "add", "origin", f"https://github.com/example/{name}")
	write(root / name / "__init__.py", "")
	write(root / name / "hooks.py", "")
	return root


def commit(root, message="fixture"):
	git(root, "add", ".")
	git(root, "commit", "-qm", message)
	return git(root, "rev-parse", "HEAD")


@pytest.fixture
def apps(tmp_path):
	frappe = repository(tmp_path / "frappe", "frappe")
	commit(frappe)
	upstream = repository(tmp_path / "upstream", "upstream")
	write(upstream / "upstream/api.py", "def original():\n    return 'before'\n")
	baseline = commit(upstream)
	custom = repository(tmp_path / "custom", "custom")
	write(
		custom / "custom/hooks.py",
		"required_apps = ['upstream']\n"
		"override_whitelisted_methods = {'upstream.api.original': 'custom.override.replacement'}\n",
	)
	write(
		custom / "custom/override.py",
		f'''def replacement():
    """
    APP: custom
    HASH: {baseline}
    REPO: example/upstream
    PATH: upstream/api.py
    METHOD: original
    """
    return "custom"
''',
	)
	commit(custom)
	return tmp_path, custom, upstream, baseline


def result(document):
	assert len(document["results"]) == 1
	return document["results"][0]


def test_contract_schema_is_published_and_versioned():
	schema = json.loads(SCHEMA.read_text())
	assert schema["properties"]["schema_version"]["const"] == 1
	assert schema["additionalProperties"] is True
	assert set(schema["$defs"]["result"]["required"]) == {
		"override_id",
		"classification",
		"discovery",
		"owner",
		"upstream",
		"annotation",
		"change_kind",
		"diff",
		"diagnostics",
	}


def test_analyze_overrides_returns_a_versioned_unchanged_result(apps):
	root, custom, upstream, baseline = apps
	document = analyze_overrides(custom, root)
	item = result(document)
	assert document["schema_version"] == 1
	assert set(json.loads(SCHEMA.read_text())["required"]) <= document.keys()
	assert document["outcome"] == "completed"
	assert document["subject"]["app"] == "custom"
	assert document["subject"]["revision"] == git(custom, "rev-parse", "HEAD")
	assert {source["app"] for source in document["sources"]} == {"frappe", "upstream"}
	assert item["classification"] == "unaffected"
	assert item["upstream"]["baseline_revision"] == baseline
	assert item["upstream"]["target_revision"] == git(upstream, "rev-parse", "HEAD")
	assert item["upstream"]["baseline_body_sha256"]
	assert item["upstream"]["baseline_body_sha256"] == item["upstream"]["target_body_sha256"]
	assert item["annotation"]["repository_identity"] == "github.com/example/upstream"
	assert set(json.loads(SCHEMA.read_text())["$defs"]["result"]["required"]) <= item.keys()


def test_analyze_overrides_does_not_import_application_modules(apps):
	root, custom, upstream, _ = apps
	write(custom / "custom/__init__.py", "raise RuntimeError('custom imported')\n")
	write(upstream / "upstream/__init__.py", "raise RuntimeError('upstream imported')\n")
	commit(custom, "make custom import fail")
	commit(upstream, "make upstream import fail")
	assert result(analyze_overrides(custom, root))["classification"] == "unaffected"


def register_module_assignment(custom):
	write(custom / "custom/hooks.py", "required_apps = ['upstream']\n")
	write(
		custom / "custom/__init__.py",
		"import upstream.api as upstream_api\n"
		"from custom.override import replacement\n"
		"upstream_api.original = replacement\n",
	)
	commit(custom, "register module assignment")


def test_analyze_overrides_exposes_module_assignment_discovery(apps):
	root, custom, _, _ = apps
	register_module_assignment(custom)
	item = result(analyze_overrides(custom, root))
	assert item["classification"] == "unaffected"
	assert item["discovery"]["kind"] == "module_assignment"
	assert item["discovery"]["file"] == "custom/__init__.py"
	assert item["discovery"]["original_reference"] == "upstream_api.original"
	assert item["upstream"]["symbol"] == "upstream.api.original"


def test_analyze_overrides_confirms_removed_module_assignment_original(apps):
	root, custom, upstream, _ = apps
	register_module_assignment(custom)
	write(upstream / "upstream/api.py", "def renamed():\n    return 'after'\n")
	commit(upstream, "remove module-assignment original")
	item = result(analyze_overrides(custom, root))
	assert item["classification"] == "affected"
	assert item["change_kind"] == "missing"
	assert item["discovery"]["kind"] == "module_assignment"


def test_analyze_overrides_keeps_an_unresolved_replacement_in_the_inventory(apps):
	root, custom, _, _ = apps
	write(custom / "custom/hooks.py", "required_apps = ['upstream']\n")
	write(
		custom / "custom/__init__.py",
		"import upstream.api as upstream_api\n"
		"upstream_api.original = missing_replacement\n",
	)
	commit(custom, "add unresolved module assignment")
	document = analyze_overrides(custom, root)
	item = result(document)
	assert document["outcome"] == "incomplete"
	assert item["classification"] == "inconclusive"
	assert item["discovery"]["kind"] == "module_assignment"
	assert item["diagnostics"][0]["code"] == "replacement_unresolved"


def test_analyze_overrides_reports_confirmed_changes(apps):
	root, custom, upstream, _ = apps
	write(upstream / "upstream/api.py", "def original():\n    return 'after'\n")
	commit(upstream, "change original")
	item = result(analyze_overrides(custom, root))
	assert item["classification"] == "affected"
	assert item["change_kind"] == "modified"
	assert "return 'before'" in item["diff"]
	assert "return 'after'" in item["diff"]


def test_analyze_overrides_confirms_a_removed_method_from_a_valid_baseline(apps):
	root, custom, upstream, _ = apps
	write(upstream / "upstream/api.py", "def renamed():\n    return 'after'\n")
	commit(upstream, "remove original")
	item = result(analyze_overrides(custom, root))
	assert item["classification"] == "affected"
	assert item["change_kind"] == "missing"
	assert item["upstream"]["baseline_body_sha256"]
	assert item["upstream"]["target_body_sha256"] is None


def test_analyze_overrides_structures_missing_annotations(apps):
	root, custom, _, _ = apps
	write(custom / "custom/override.py", "def replacement():\n    return 'custom'\n")
	commit(custom, "remove annotation")
	document = analyze_overrides(custom, root)
	item = result(document)
	assert document["outcome"] == "completed_with_findings"
	assert item["classification"] == "invalid_annotation"
	assert item["annotation"]["fields"] == {}
	assert item["diagnostics"][0]["code"] == "annotation_missing"


def test_analyze_overrides_does_not_compare_invalid_annotations(apps):
	root, custom, _, _ = apps
	path = custom / "custom/override.py"
	path.write_text(path.read_text().replace("APP: custom", "APP: wrong"))
	commit(custom, "invalidate annotation")
	item = result(analyze_overrides(custom, root))
	assert item["classification"] == "invalid_annotation"
	assert item["change_kind"] is None
	assert item["diff"] == ""
	assert any(diagnostic["code"] == "annotation_invalid" for diagnostic in item["diagnostics"])


def test_analyze_overrides_ignores_annotations_that_are_not_registered(apps):
	root, custom, _, baseline = apps
	write(
		custom / "custom/orphan.py",
		f'''def annotated_but_not_registered():
	"""
	APP: custom
	HASH: {baseline}
	REPO: example/upstream
	PATH: upstream/api.py
	METHOD: original
	"""
	return "orphan"
''',
	)
	commit(custom, "add orphan annotation")
	document = analyze_overrides(custom, root)
	assert len(document["results"]) == 1
	assert document["results"][0]["owner"]["symbol"] == "custom.override.replacement"


def test_analyze_overrides_reports_an_ambiguous_historical_symbol_as_inconclusive(apps):
	root, custom, upstream, baseline = apps
	write(
		upstream / "upstream/api.py",
		"def original():\n    return 'first'\n\ndef original():\n    return 'second'\n",
	)
	git(upstream, "add", ".")
	git(upstream, "commit", "--amend", "--no-edit", "-q")
	ambiguous = git(upstream, "rev-parse", "HEAD")
	path = custom / "custom/override.py"
	path.write_text(path.read_text().replace(baseline, ambiguous))
	commit(custom, "point to ambiguous baseline")
	write(upstream / "upstream/api.py", "def original():\n    return 'after'\n")
	commit(upstream, "resolve ambiguity")
	document = analyze_overrides(custom, root)
	item = result(document)
	assert document["outcome"] == "incomplete"
	assert item["classification"] == "inconclusive"
	assert item["change_kind"] is None
	assert any(diagnostic["code"] == "comparison_inconclusive" for diagnostic in item["diagnostics"])


def test_analyze_overrides_reports_an_inaccessible_source_as_technical_error(apps, monkeypatch):
	root, custom, _, _ = apps

	class InaccessibleSources:
		def __init__(self, repo_url, revision, *, checkout=None):
			self.revision = revision

		@property
		def paths(self):
			raise UpstreamFetchError("source unavailable")

	monkeypatch.setattr(override_analysis, "RevisionSources", InaccessibleSources)
	document = analyze_overrides(custom, root)
	item = result(document)
	assert document["outcome"] == "incomplete"
	assert item["classification"] == "technical_error"
	assert item["change_kind"] is None
	assert any(diagnostic["code"] == "source_access_error" for diagnostic in item["diagnostics"])


def test_analyze_overrides_rejects_a_revision_different_from_checkout_head(apps):
	root, custom, _, _ = apps
	document = analyze_overrides(custom, root, revisions={"custom": "a" * 40})
	assert document["outcome"] == "incomplete"
	assert document["results"] == []
	assert document["errors"][0]["code"] == "analysis_setup_error"


def test_analyze_overrides_rejects_a_dirty_checkout(apps):
	root, custom, _, _ = apps
	write(custom / "custom/uncommitted.py", "value = 1\n")
	document = analyze_overrides(custom, root)
	assert document["outcome"] == "incomplete"
	assert document["results"] == []
	assert "uncommitted changes" in document["errors"][0]["message"]


def test_analyze_overrides_cli_writes_only_json_to_stdout(apps, capsys):
	root, custom, _, _ = apps
	assert main(["--app", str(custom), "--apps-root", str(root)]) == 0
	document = json.loads(capsys.readouterr().out)
	assert document["schema_version"] == 1
	assert document["outcome"] == "completed"


def test_analyze_overrides_cli_writes_json_to_the_selected_file(apps, tmp_path, capsys):
	root, custom, _, _ = apps
	output = tmp_path / "analysis.json"
	assert main(["--app", str(custom), "--apps-root", str(root), "--output", str(output)]) == 0
	assert capsys.readouterr().out == ""
	assert json.loads(output.read_text())["outcome"] == "completed"


def test_analyze_overrides_cli_returns_two_with_a_valid_error_document(tmp_path, capsys):
	assert main(["--app", str(tmp_path / "missing"), "--apps-root", str(tmp_path)]) == 2
	document = json.loads(capsys.readouterr().out)
	assert document["outcome"] == "incomplete"
	assert document["errors"][0]["code"] == "analysis_setup_error"
