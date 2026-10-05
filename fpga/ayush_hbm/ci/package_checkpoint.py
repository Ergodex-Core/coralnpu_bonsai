"""Invoke only the hash-locked local HDK packager; no AWS operations."""
import importlib.util
import os
from pathlib import Path
import sys

hdk = Path(os.environ['AWS_FPGA_REPO_DIR'])
spec = importlib.util.spec_from_file_location(
    'hdk_packager',
    hdk / 'hdk/common/shell_stable/build/scripts/aws_build_dcp_from_cl.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.generate_dcp_tarball(
    'cl_coralnpu_hbm', sys.argv[1], 'A1', 'B2', 'C0', 'H3'
)
