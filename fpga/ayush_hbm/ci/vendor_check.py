"""Verify the tool selected after vendor environment setup, before every stage."""
from inputs import PINS, capture
from process import require

version = capture(['vivado', '-version'])
require(
    'Vivado v' + PINS['vivado_version'] in version
    and PINS['vivado_build'] in version,
    'Vendor setup selected an unsupported Vivado version/build'
)
print('HBM_VENDOR_VERSION_VERIFIED')
