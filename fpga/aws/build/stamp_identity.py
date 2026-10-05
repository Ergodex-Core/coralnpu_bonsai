"""Proposed strict Git-stamp-aware RTL identity check; no artifact writes.

Only the five numeric kscm CSR literals may be normalized, and only after each
has matched the actual checked-out Git commit. Every other byte remains covered
by the existing reference hashes. The caller must retain the returned receipt
and keep its existing source/configuration, simulation and routed gates.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess


class StampIdentityError(ValueError):
    """A source identity, stamp shape or pinned byte comparison failed."""


# Exact CIRCT expression spelling observed in the pinned and current emission.
# Match numeric tokens only; never globally replace revision-looking strings.
STAMP = re.compile(
    rb"(?P<prefix>\(kscm(?P<index>[0-4])En \? 32'h)"
    rb"(?P<word>[0-9A-F]{1,8})(?P<suffix> : 32'h0\))"
)


def require(condition, message):
    if not condition:
        raise StampIdentityError(message)


def revision(value, name):
    require(
        isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value),
        f'{name} must be a full lowercase Git SHA'
    )
    return value


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _git_head(repo):
    result = subprocess.check_output([
        'git', '--no-optional-locks', '-C',
        str(repo), 'rev-parse', 'HEAD'
    ],
                                     text=True,
                                     stderr=subprocess.PIPE,
                                     timeout=30).strip()
    return revision(result, 'checkout SHA')


def source_identity(repo, *, pr_head_sha=None, github_checkout_sha=None):
    """Bind stamp checks to HEAD, never substitute the distinct PR head SHA."""
    head = _git_head(repo)
    if pr_head_sha is not None:
        revision(pr_head_sha, 'PR head SHA')
    if github_checkout_sha is not None:
        revision(github_checkout_sha, 'GitHub checkout SHA')
        require(
            github_checkout_sha == head,
            'GitHub checkout SHA differs from actual Git HEAD'
        )
    return {
        'checkout_sha': head,
        'pr_head_sha': pr_head_sha,
        'github_checkout_sha': github_checkout_sha
    }


def _normalize_stamp(data, *, checkout_sha, reference_sha, label):
    """Return a separate in-memory byte string; input is immutable bytes."""
    revision(checkout_sha, 'checkout SHA')
    revision(reference_sha, 'reference SHA')
    require(isinstance(data, bytes), f'{label}: expected immutable bytes')
    matches = list(STAMP.finditer(data))
    indices = [int(match['index']) for match in matches]
    require(
        len(matches) == 5 and sorted(indices) == list(range(5)),
        f'{label}: require exactly one literal for each kscm0..4'
    )
    for match in matches:
        index = int(match['index'])
        expected = (int(checkout_sha, 16) >> (32 * index)) & 0xffffffff
        # FIRRTL renders uppercase, minimal-width hexadecimal literals.
        require(
            match['word'] == format(expected, 'X').encode('ascii'),
            f'{label}: kscm{index} differs from checked-out source SHA'
        )

    def replace(match):
        index = int(match['index'])
        value = (int(reference_sha, 16) >> (32 * index)) & 0xffffffff
        return match['prefix'] + format(value,
                                        'X').encode('ascii') + match['suffix']

    return STAMP.sub(replace, data)


def _read_artifact(path):
    path = Path(path)
    require(
        path.is_file() and not path.is_symlink(), f'Invalid artifact: {path}'
    )
    return path.read_bytes()


def _inventory_hash(inventory):
    return sha256(
        json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode()
    )


def validate_emission(
    repo,
    top_path,
    include_dir,
    pins,
    *,
    pr_head_sha=None,
    github_checkout_sha=None
):
    """Check the original pins after validating the checkout-specific stamp.

    This verifies generated-byte identity only. It does not qualify a checkpoint,
    firmware, simulation, toolchain, or physical execution.
    """
    identity = source_identity(
        repo, pr_head_sha=pr_head_sha, github_checkout_sha=github_checkout_sha
    )
    reference = revision(pins['coral_reference_commit'], 'reference SHA')
    for name in ('reference_emitted_rtl_sha256',
                 'reference_include_inventory_sha256'):
        require(
            isinstance(pins[name], str)
            and re.fullmatch(r'[0-9a-f]{64}', pins[name]),
            f'Invalid pinned hash: {name}'
        )
    top = _read_artifact(top_path)
    normalized_top = _normalize_stamp(
        top,
        checkout_sha=identity['checkout_sha'],
        reference_sha=reference,
        label='RvvCoreMiniAxi.sv'
    )
    require(
        sha256(normalized_top) == pins['reference_emitted_rtl_sha256'],
        'RTL differs from pinned reference beyond the verified Git stamp'
    )
    include_dir = Path(include_dir)
    require(
        include_dir.is_dir() and not include_dir.is_symlink(),
        'Invalid include directory'
    )
    files = sorted(include_dir.iterdir())
    require(
        any(path.name == 'Csr.sv' for path in files), 'Missing include/Csr.sv'
    )
    raw_inventory, normalized_inventory = {}, {}
    for path in files:
        data = _read_artifact(path)
        raw_inventory[path.name] = sha256(data)
        if path.name == 'Csr.sv':
            data = _normalize_stamp(
                data,
                checkout_sha=identity['checkout_sha'],
                reference_sha=reference,
                label='include/Csr.sv'
            )
        else:
            require(
                not STAMP.search(data),
                f'Unexpected stamp outside Csr.sv: {path.name}'
            )
        normalized_inventory[path.name] = sha256(data)
    normalized_inventory_sha = _inventory_hash(normalized_inventory)
    require(
        normalized_inventory_sha == pins['reference_include_inventory_sha256'],
        'Include inventory differs from pinned reference beyond the verified Git stamp'
    )
    require(
        _git_head(repo) == identity['checkout_sha'],
        'Checkout changed during validation'
    )
    return {
        'schema_version': 1,
        'scope':
        'generated RTL identity only; no routed or physical qualification',
        **identity, 'reference_sha': reference,
        'normalization':
        'five verified kscm literals in top and include/Csr.sv; memory only',
        'raw_top_sha256': sha256(top),
        'reference_stamp_top_sha256': sha256(normalized_top),
        'raw_include_sha256': raw_inventory,
        'raw_include_inventory_sha256': _inventory_hash(raw_inventory),
        'reference_stamp_include_inventory_sha256': normalized_inventory_sha,
        'pins': {
            name: pins[name]
            for name in (
                'reference_emitted_rtl_sha256',
                'reference_include_inventory_sha256'
            )
        }
    }
