"""Reject ambient SDK dependency drift before credentials or service access."""
from importlib.metadata import version
import os
import sys

EXPECTED = {
    "botocore": "1.43.108",
    "jmespath": "1.1.0",
    "python-dateutil": "2.9.0.post0",
    "urllib3": "2.8.0",
    "six": "1.17.0"
}


def verify_sdk_runtime():
    if not (3, 10) <= sys.version_info[:2] < (3, 15):
        raise ValueError(
            "controller Python runtime is outside the reviewed supported range"
        )
    for package, expected in EXPECTED.items():
        if version(package) != expected:
            raise ValueError(
                "SDK dependency differs from requirements-controller.lock: " +
                package
            )


def isolated_session():
    """A pinned SDK session that never reads ambient profiles or custom models."""
    verify_sdk_runtime()
    from botocore.session import Session
    session = Session()
    store = session.get_component("config_store")
    for name, value in {"config_file": os.devnull, "credentials_file":
                        os.devnull, "profile": None, "data_path": None,
                        "ca_bundle": None, "defaults_mode": "legacy"}.items():
        store.set_config_variable(name, value)
    return session
