# SPDX-License-Identifier: Apache-2.0
"""Opt-in workaround for kauldron1.4.2's unsupported Mac float128 annotation.

Run only inside an isolated virtualenv. No arithmetic dtype is substituted.
Linux platforms with np.float128 are unchanged.
"""
import argparse,importlib.metadata,sys
from pathlib import Path
import numpy as np
p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');a=p.parse_args()
if hasattr(np,'float128'):print('No change needed');raise SystemExit
if sys.prefix==sys.base_prefix:raise SystemExit('Refusing to edit a system environment')
if importlib.metadata.version('kauldron')!='1.4.2':raise SystemExit('Review a different kauldron version separately')
path=Path(importlib.metadata.distribution('kauldron').locate_file('kauldron/ktyping/dtypes.py'))
old=path.read_text();needle='float128 = NpDType(np.float128)'
replacement='if hasattr(np, "float128"):\n  float128 = NpDType(np.float128)  # Omit unsupported annotation, no dtype alias.'
if '\nif hasattr(np, "float128"):' in old:print('Already patched');raise SystemExit
if needle not in old:raise SystemExit('Unexpected package source')
if not a.apply:raise SystemExit('Use --apply to omit only the unsupported annotation')
backup=path.with_suffix('.py.original');backup.write_text(old)
path.write_text(old.replace(needle,replacement));print('Patched annotation in isolated environment')
