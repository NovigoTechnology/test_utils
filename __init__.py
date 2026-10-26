from importlib.metadata import PackageNotFoundError, version

from test_utils.utils.conftest import db_instance, monkeymodule
from test_utils.utils.create_partition import create_partition
from test_utils.utils.customize import add_customization_hash, load_customizations

try:
	__version__ = version("test_utils")
except PackageNotFoundError:
	__version__ = "unknown"
