import importlib.util
from pathlib import Path

import pytest

from test_utils.pre_commit import track_overrides
from test_utils.utils.track_overrides import compare_method_diff, extract_method

SOURCE = """
class One:
    @classmethod
    async def validate(cls) -> str:
        return "one"

class Two:
    def validate(self) -> str:
        return "two"

def top_level(value: str) -> str:
    return value
"""


def test_ast_extractor_supports_scope_async_returns_and_decorators():
	assert 'return "one"' in extract_method(SOURCE, "One.validate")
	assert "@classmethod" in extract_method(SOURCE, "One.validate")
	assert 'return "two"' in extract_method(SOURCE, "Two.validate")
	assert "return value" in extract_method(SOURCE, "top_level")
	assert extract_method(SOURCE, "validate") is None
	assert extract_method(SOURCE, "Missing.validate") is None


def test_unique_legacy_method_names_are_supported():
	assert 'return "one"' in extract_method(SOURCE.split("class Two")[0], "validate")


def test_unrelated_class_changes_do_not_change_tracked_method():
	old = extract_method(SOURCE, "One.validate")
	new = extract_method(SOURCE.replace('return "two"', 'return "changed"'), "One.validate")
	assert not compare_method_diff(old, new)


def test_removed_method_produces_a_diff():
	method = extract_method(SOURCE, "One.validate")
	assert '-        return "one"' in compare_method_diff(method, None)
	assert "could not be resolved" in compare_method_diff(None, None)


@pytest.mark.parametrize("action", [False, True])
def test_trackers_handle_qualified_names_and_removed_methods(tmp_path, monkeypatch, action):
	module = track_overrides
	if action:
		path = Path(__file__).resolve().parents[2] / "actions/track_overrides/track_overrides.py"
		spec = importlib.util.spec_from_file_location("track_overrides_action", path)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	(tmp_path / "override.py").write_text('''
def custom():
    """
    APP: nagus
    HASH: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    REPO: https://github.com/owner/repo-with-hyphens
    PATH: upstream/controller.py
    METHOD: One.validate
    """
    pass
''')
	monkeypatch.setattr(module, "get_last_commit_hash_for_file_in_branch", lambda *args: "b" * 40)
	monkeypatch.setattr(
		module,
		"download_file_from_commit",
		lambda repo, sha, path: SOURCE if sha == "a" * 40 else "class One: pass",
	)
	if action:
		changes = module.compare_commit_hashes(str(tmp_path))
	else:
		changes, unverified = module.check_tracked_methods(str(tmp_path), "develop")
		assert not unverified
	assert len(changes) == 1
	assert "One.validate" in str(changes)
	assert '-        return "one"' in str(changes)
