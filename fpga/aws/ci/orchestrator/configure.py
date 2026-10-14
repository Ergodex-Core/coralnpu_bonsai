"""Materialize nonsecret operator configuration without embedding it in source."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from validate_bundle import activation_errors


def configure(environment):
    if environment.get("FPGA_CI_BACKEND") != "ssm":
        raise ValueError("SSM backend is not selected")
    raw = environment.get("FPGA_CI_CONFIG_JSON", "")
    if not raw or len(raw.encode()) > 32768:
        raise ValueError(
            "operator activation configuration is missing or too large"
        )
    config = json.loads(raw)
    template = json.loads((ROOT / "config.example.json").read_text())
    if not isinstance(config, dict) or set(config) != set(template):
        raise ValueError(
            "operator configuration fields differ from the reviewed schema"
        )
    errors = activation_errors(config)
    if errors:
        raise ValueError("ACTIVATION BLOCKED: " + "; ".join(errors))
    target = Path(environment["RUNNER_TEMP"]) / "coralnpu-activation.json"
    if not target.is_absolute() or any(ch in str(target) for ch in "\r\n\x00"):
        raise ValueError("invalid runner temporary path")
    fd = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, sort_keys=True)
    with open(environment["GITHUB_ENV"], "a") as stream:
        stream.write("CORALNPU_ACTIVATION_CONFIG=" + str(target) + "\n")
    return target


if __name__ == "__main__":
    try:
        configure(os.environ)
    except Exception as exc:
        raise SystemExit(
            str(exc) if isinstance(exc, ValueError) else
            "Configuration rejected: " + type(exc).__name__
        ) from None
