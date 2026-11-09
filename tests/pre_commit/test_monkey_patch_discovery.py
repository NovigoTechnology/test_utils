from test_validate_override_annotations import annotation, function_source, hooks, validate, write
from test_validate_override_annotations import bench as bench


def test_discovers_the_four_direct_nagus_monkey_patches_without_annotations(bench):
	write(
		bench / "frappe/frappe/core/doctype/session_default_settings/session_default_settings.py",
		"def clear_session_defaults(): pass\n",
	)
	write(bench / "erpnext/erpnext/setup/utils.py", "def get_exchange_rate(): pass\n")
	write(
		bench / "erpnext/erpnext/controllers/trends.py",
		"def based_wise_columns_query(): pass\ndef get_data(): pass\n",
	)
	write(
		bench / "erpnext/erpnext/accounts/report/sales_invoice_trends/sales_invoice_trends.py",
		"from erpnext.controllers.trends import get_data\n",
	)
	write(bench / "nagus/nagus/api/user.py", "def onlogut(): pass\n")
	write(bench / "nagus/nagus/utils/utils.py", "def get_exchange_rate_nagus(): pass\n")
	write(
		bench / "nagus/nagus/controllers/trends.py",
		"def based_wise_columns_query(): pass\ndef get_data(): pass\n",
	)
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import frappe.core.doctype.session_default_settings.session_default_settings
		import nagus.api.user
		frappe.core.doctype.session_default_settings.session_default_settings.clear_session_defaults = nagus.api.user.onlogut

		import erpnext.setup.utils
		from nagus.utils.utils import get_exchange_rate_nagus
		erpnext.setup.utils.get_exchange_rate = get_exchange_rate_nagus

		import erpnext.accounts.report.sales_invoice_trends.sales_invoice_trends
		import erpnext.controllers.trends
		import nagus.controllers.trends
		erpnext.controllers.trends.based_wise_columns_query = nagus.controllers.trends.based_wise_columns_query
		erpnext.accounts.report.sales_invoice_trends.sales_invoice_trends.get_data = nagus.controllers.trends.get_data
		""",
	)
	result = validate(bench)
	assert result.checked == 4
	assert len(result.errors) == 4
	assert all("Missing annotation fields" in error for error in result.errors)
	assert all("installed at" in error and "nagus/__init__.py" in error for error in result.errors)
	assert any(
		"nagus.controllers.trends.get_data -> erpnext.controllers.trends.get_data" in error
		for error in result.errors
	)


def test_static_import_forms_and_upstream_reexports_resolve(bench):
	write(bench / "erpnext/erpnext/accounts/utils.py", "def get_coa(): pass\n")
	write(bench / "erpnext/erpnext/controllers/trends.py", "def get_data(): pass\n")
	write(
		bench / "erpnext/erpnext/accounts/report/sales_invoice_trends/sales_invoice_trends.py",
		"from erpnext.controllers.trends import get_data\n",
	)
	write(
		bench / "nagus/nagus/overrides.py",
		function_source(annotation(), name="get_coa")
		+ function_source(
			annotation(path="erpnext/controllers/trends.py", method="get_data"),
			name="get_data",
		),
	)
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.utils as accounts
		from erpnext.accounts.report.sales_invoice_trends import sales_invoice_trends as report
		from nagus.overrides import get_coa, get_data
		accounts.get_coa = get_coa
		report.get_data = get_data
		""",
	)
	result = validate(bench)
	assert result.checked == 2
	assert not result.errors


def test_only_entrypoints_and_reachable_unconditional_local_imports_are_scanned(bench):
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/patches.py",
		"import erpnext.accounts.utils as upstream\n"
		"from nagus.overrides import get_coa\n"
		"upstream.get_coa = get_coa\n",
	)
	write(
		bench / "nagus/nagus/unreachable.py",
		"import erpnext.accounts.utils as upstream\nupstream.get_coa = missing\n",
	)
	write(
		bench / "nagus/nagus/conditional.py",
		"import erpnext.accounts.utils as upstream\nupstream.get_coa = missing\n",
	)
	write(
		bench / "nagus/nagus/dormant.py",
		"import erpnext.accounts.utils as upstream\nupstream.get_coa = missing\n",
	)
	write(
		bench / "nagus/nagus/__init__.py",
		"import nagus.patches\n"
		"dormant = object()\n"
		"from nagus import dormant\n"
		"if enabled:\n    import nagus.conditional\n",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_non_function_dynamic_instance_class_and_nested_cases_are_ignored(bench):
	write(bench / "erpnext/erpnext/__init__.py", "class Client: pass\nclient = Client()\n")
	write(bench / "erpnext/erpnext/client.py", "def callback(): pass\n")
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.utils as upstream
		from erpnext import client
		from nagus.overrides import get_coa
		upstream.enabled = True
		client.callback = get_coa
		upstream.Controller.validate = get_coa
		upstream.get_coa = factory().handler
		if enabled:
		    import erpnext.accounts.utils as conditional
		conditional.get_coa = get_coa
		def install_later():
		    upstream.get_coa = get_coa
		upstream.get_coa = get_coa
		""",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors


def test_missing_owner_original_and_replacement_are_retained(bench):
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(bench / "erpnext/erpnext/accounts/other.py", "def existing(): pass\n")
	write(
		bench / "nagus/nagus/__init__.py",
		"""
		import erpnext.accounts.removed as removed
		import erpnext.accounts.utils as upstream
		import erpnext.accounts.other as other
		from nagus.overrides import get_coa
		removed.get_coa = get_coa
		upstream.removed_original = get_coa
		other.existing = missing_replacement
		""",
	)
	result = validate(bench)
	assert result.checked == 0
	assert len(result.errors) == 3
	assert any(
		"Cannot resolve monkey-patched owner module erpnext.accounts.removed" in error
		for error in result.errors
	)
	assert any("removed_original" in error and "removed or renamed" in error for error in result.errors)
	assert any("Cannot resolve replacement function" in error for error in result.errors)


def test_hook_and_module_assignment_validate_the_same_declarations_once(bench):
	hooks(
		bench,
		"override_whitelisted_methods = {'erpnext.accounts.utils.get_coa': 'nagus.overrides.get_coa'}",
	)
	write(bench / "nagus/nagus/overrides.py", function_source(annotation()))
	write(
		bench / "nagus/nagus/__init__.py",
		"import erpnext.accounts.utils as upstream\n"
		"from nagus.overrides import get_coa\n"
		"upstream.get_coa = get_coa\n",
	)
	result = validate(bench)
	assert result.checked == 1
	assert not result.errors
