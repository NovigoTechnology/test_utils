"""Mutable-default projections retain their owning callable without tainting copies."""

import pytest
from test_validate_override_annotations import bench as bench
from test_wildcard_controllers import module, resolver

from test_utils.utils.override_annotations import ResolutionError


@pytest.mark.parametrize(
	"projection",
	[
		"target.__kwdefaults__['option']",
		"target.__kwdefaults__.get('option')",
		"list(target.__kwdefaults__.values())[0]",
		"target.__kwdefaults__.copy()['option']",
	],
)
def test_mutating_a_shared_mutable_default_invalidates_its_owner(bench, projection):
	module(
		bench, "api", "def target(*, option=[]): return option\nalias = " + projection + "\nalias.append(2)"
	)
	with pytest.raises(ResolutionError, match="Ambiguous"):
		resolver(bench).resolve("erpnext.api.target")


def test_replacing_a_slot_in_a_shallow_copy_does_not_rebind_function_defaults(bench):
	module(
		bench,
		"api",
		"def target(*, option=[]): return option\ncopy = target.__kwdefaults__.copy()\ncopy['option'] = [2]",
	)
	assert resolver(bench).resolve("erpnext.api.target").node.name == "target"
