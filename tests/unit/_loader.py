"""Loads the service modules for testing.

auth/app.py and director/app.py are both named `app`, so a plain import would
have them collide in sys.modules. They are loaded here under distinct names.
"""

import importlib.util
import sys
from pathlib import Path

from prometheus_client import REGISTRY
from prometheus_client.metrics import MetricWrapperBase

ROOT = Path(__file__).resolve().parents[2]


def _load(module_name: str, service: str, filename: str):
    service_dir = ROOT / service
    # auth/app.py does `from models import ...`, so its own directory has to be
    # importable.
    if str(service_dir) not in sys.path:
        sys.path.insert(0, str(service_dir))
    spec = importlib.util.spec_from_file_location(module_name, service_dir / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _unregister_metrics(module):
    """Drop this module's metrics from the process-wide registry.

    Each service defines the same metric names at module scope. That is fine in
    production, where every service is its own process. Here two services share
    one process, so the second load would fail with DuplicateTimeseries.
    """
    for value in vars(module).values():
        if isinstance(value, MetricWrapperBase):
            try:
                REGISTRY.unregister(value)
            except KeyError:
                pass


auth_models = _load("iep_auth_models", "auth", "models.py")
auth_app = _load("iep_auth_app", "auth", "app.py")
_unregister_metrics(auth_app)
director_app = _load("iep_director_app", "director", "app.py")
_unregister_metrics(director_app)
