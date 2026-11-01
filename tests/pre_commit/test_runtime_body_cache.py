"""Runtime source views share work while remaining isolated and collectible."""

import ast
import gc
import weakref

from test_utils.utils import python_source
from test_utils.utils.override_annotations import SourceResolver

SOURCE = """from typing import TYPE_CHECKING
class Controller:
    if TYPE_CHECKING:
        def typing_only(self): pass
    def value(self): return 1
"""


def test_runtime_views_and_namespace_checks_build_each_tree_once(monkeypatch):
	tree = ast.parse(
		"from typing import TYPE_CHECKING\n"
		+ "\n".join(
			f"class Controller{index}:\n"
			"    if TYPE_CHECKING:\n        def typing_only(self): pass\n"
			"    def value(self): return 1\n"
			for index in range(12)
		)
	)
	anchor, *owners = tree.body
	anchor_scans = 0
	original = python_source.scope_bindings

	def counted(statements):
		nonlocal anchor_scans
		if len(statements) == 1 and statements[0] is anchor:
			anchor_scans += 1
		return original(statements)

	monkeypatch.setattr(python_source, "scope_bindings", counted)
	for owner in owners:
		assert [node.name for node in python_source.runtime_body(tree, owner)] == ["value"]
		list(python_source.executable_nodes(owner, tree=tree))
		assert not SourceResolver([]).dynamic_namespace([python_source.runtime_body(tree)], {"value"}, tree)
	assert anchor_scans == 1


def test_mutating_a_returned_view_does_not_change_another_view_or_the_ast():
	tree = ast.parse(SOURCE)
	owner = tree.body[1]
	original = ast.dump(tree, include_attributes=True)
	body = python_source.runtime_body(tree, owner)
	body.clear()
	body.append(ast.Pass())
	assert [node.name for node in python_source.runtime_body(tree, owner)] == ["value"]
	module_body = python_source.runtime_body(tree)
	expected = module_body.copy()
	module_body.clear()
	assert python_source.runtime_body(tree) == expected
	assert ast.dump(tree, include_attributes=True) == original


def test_guard_contexts_and_separately_parsed_trees_do_not_share_views():
	tree = ast.parse(SOURCE)
	owner = tree.body[1]
	assert python_source.runtime_body(tree, owner) == [owner.body[1]]
	assert python_source.runtime_body(owner) == owner.body
	assert python_source.runtime_body(tree, owner) == [owner.body[1]]
	other_tree = ast.parse(SOURCE)
	other_owner = other_tree.body[1]
	other_body = python_source.runtime_body(other_tree, other_owner)
	assert other_body == [other_owner.body[1]]
	assert other_body[0] is not python_source.runtime_body(tree, owner)[0]


def test_a_returned_class_body_does_not_keep_its_root_tree_alive():
	tree = ast.parse(SOURCE)
	owner = tree.body[1]
	body = python_source.runtime_body(tree, owner)
	root = weakref.ref(tree)
	del tree, owner
	gc.collect()
	assert root() is None
	assert [node.name for node in body] == ["value"]
