import pytest

from test_utils.utils.override_metadata import github_repository, repository_identity


@pytest.mark.parametrize(
	"value",
	[
		"NovigoTechnology/erpnext",
		"NovigoTechnology/erpnext.git",
		"https://github.com/NovigoTechnology/erpnext",
		"https://github.com/NovigoTechnology/erpnext.git/",
		"git@github.com:NovigoTechnology/erpnext.git",
		"ssh://git@github.com/NovigoTechnology/erpnext.git",
		"git://github.com/NovigoTechnology/erpnext.git",
	],
)
def test_github_repository_references_share_one_identity(value):
	assert repository_identity(value) == "github.com/novigotechnology/erpnext"
	assert github_repository(value) == ("novigotechnology", "erpnext")


@pytest.mark.parametrize(
	"value",
	[
		"",
		"erpnext",
		"github.com/NovigoTechnology/erpnext",
		"NovigoTechnology/erpnext/extra",
		"../erpnext",
		"NovigoTechnology/..",
		"NovigoTechnology/",
		"NovigoTechnology//erpnext",
		"NovigoTechnology/erpnext?ref=main",
		"NovigoTechnology/erpnext#readme",
	],
)
def test_invalid_github_repository_references_are_rejected(value):
	with pytest.raises(ValueError, match="REPO must"):
		repository_identity(value)


def test_short_repository_references_are_always_github():
	assert repository_identity("gitlab/erpnext") == "github.com/gitlab/erpnext"
	assert repository_identity("https://gitlab.com/owner/repository") == "gitlab.com/owner/repository"
	with pytest.raises(ValueError, match=r"github\.com owner/repository"):
		github_repository("https://gitlab.com/owner/repository")
