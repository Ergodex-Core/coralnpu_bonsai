#!/usr/bin/env python3
"""Build Ayush's fixed Coral/HBM design in a fresh directory; default is dry-run.

No upload, AFI creation, hardware execution, or workspace replacement is exposed.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile

from build_fixture import build as build_fixture
from generate_cl import generate
from inputs import PINS, capture, dependency_inventory, sha, tool_versions, tree_hashes
from inputs import validate_coral, verify_dependency_lock
from process import check_log, require, run
from reports import qualify

CI = Path(__file__).resolve().parent


def source_check():
    """Cheap local integrity/configuration check; never initializes vendor tools."""
    provenance = json.loads((CI.parent / 'PROVENANCE.json').read_text())
    require(
        len(provenance['files']) == 36, 'Unexpected source handoff inventory'
    )
    top = (CI /
           'templates/cl_coralnpu_hbm/design/cl_coralnpu_hbm.sv').read_text()
    require(
        re.search(r'parameter\s+EN_DDR\s*=\s*0', top),
        'DDR must remain disabled'
    )
    require('.HBM_PRESENT(1)' in top, 'HBM must be enabled')
    wrapper = (CI / 'templates/cl_coralnpu_hbm/design/cl_hbm_wrapper.sv'
               ).read_text()
    require(
        'o_hbm_ready <= &hbm_ready_q;' in wrapper,
        'HBM ready must combine both stacks'
    )
    require(
        PINS['clock_recipes'] == ['A1', 'B2', 'C0', 'H3'],
        'Clock recipe mismatch'
    )
    require(
        PINS['hbm_bytes'] == 16 << 30 and not PINS['ddr_enabled'],
        'Memory profile mismatch'
    )
    manifest_names = []
    for line in (CI.parent / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        require(name not in manifest_names, 'Duplicate source manifest path')
        manifest_names.append(name)
        path = CI.parent / name
        require(
            not Path(name).is_absolute() and '..' not in Path(name).parts,
            'Unsafe source manifest path'
        )
        require(sha(path) == expected, 'Source manifest mismatch: ' + name)
    actual = set(tree_hashes(CI.parent)) - {'SHA256SUMS'}
    require(
        set(manifest_names) == actual,
        'Source manifest inventory is incomplete'
    )
    return {
        'source_check': 'PASS',
        'pins': PINS,
        'hardware_calibration': 'NOT_RUN',
        'physical_inference': 'NOT_RUN',
        'licensed_build': 'NOT_RUN'
    }


def generated_inputs(root):
    """Hash generated design inputs; reports/products are recorded separately."""
    result = {}
    folders = (
        'cl_coralnpu_hbm/design', 'cl_coralnpu_hbm/rtl',
        'cl_coralnpu_hbm/build/scripts', 'cl_coralnpu_hbm/build/constraints',
        'tests', 'ip/fabric'
    )
    suffixes = {
        '.sv', '.svh', '.vh', '.v', '.tcl', '.xdc', '.py', '.sh', '.S', '.ld',
        '.elf', '.xci', '.bd', '.mem', '.coe'
    }
    files = list(root.glob('*'))
    for folder in folders:
        files.extend((root / folder).rglob('*'))
    for path in sorted(files):
        if path.is_file(
        ) and path.suffix in suffixes and path.name != 'generated_cl_clocks_aws.xdc':
            result[str(path.relative_to(root))] = sha(path)
    return result


def validate_output(output, coral, hdk):
    require(
        output.is_absolute() and not output.exists()
        and not output.is_symlink(), 'Output must be a new absolute directory'
    )
    for root in (coral, hdk, CI.parent):
        require(
            not output.is_relative_to(root)
            and not root.is_relative_to(output),
            'Output must be separate from source, integration and HDK trees'
        )
    for path in (output, hdk):
        require(
            re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)),
            'HDK paths must be simple absolute paths without shell metacharacters'
        )
    require(output.parent.is_dir(), 'Output parent must already exist')


def verify_package(package, fingerprint):
    with tarfile.open(package) as archive:
        entries = archive.getmembers()
        require(
            all(e.isfile() or e.isdir() for e in entries),
            'Unexpected tar member type'
        )
        names = [e.name for e in entries]
        require(len(set(names)) == len(names), 'Duplicate tar members')
        require(
            all(
                not Path(n).is_absolute() and '..' not in Path(n).parts
                for n in names
            ), 'Unsafe tar member path'
        )
        dcps = [e for e in entries if e.name.endswith('.dcp')]
        require(len(dcps) == 1, 'Expected exactly one packaged DCP')
        import hashlib
        with archive.extractfile(dcps[0]) as stream:
            require(
                hashlib.file_digest(stream,
                                    'sha256').hexdigest() == fingerprint,
                'Packaged checkpoint differs from qualified checkpoint'
            )
        manifests = [e for e in entries if e.name.endswith('manifest.txt')]
        require(len(manifests) == 1, 'Missing or ambiguous AWS manifest')
        manifest = archive.extractfile(manifests[0]).read().decode()
        require(
            re.search(r'^clock_recipe_hbm=H3\s*$', manifest, re.M),
            'Package HBM recipe must be H3'
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    for mode in ('dry-run', 'inventory', 'preflight', 'generate-only',
                 'build'):
        modes.add_argument('--' + mode, action='store_true')
    parser.add_argument('--coral-repo', type=Path)
    parser.add_argument('--hdk-root', type=Path)
    parser.add_argument('--dependency-lock', type=Path)
    parser.add_argument('--dependency-lock-sha256')
    parser.add_argument('--output', type=Path)
    parser.add_argument(
        '--tag',
        default=dt.datetime.now(dt.timezone.utc).strftime('%Y_%m_%d-%H%M%S')
    )
    parser.add_argument('--bazel', default='bazel')
    parser.add_argument('--verilator', default='verilator')
    parser.add_argument('--clang', default='clang-18')
    parser.add_argument('--linker', default='ld.lld-18')
    args = parser.parse_args(argv)
    source = source_check()
    if args.dry_run or not any(
        (args.inventory, args.preflight, args.generate_only, args.build)):
        print(
            json.dumps({
                'mode':
                'dry-run',
                'source':
                source_check(),
                'stages': [
                    'verify reviewed dependency lock and versions',
                    'create fresh output', 'generate pinned RTL and templates',
                    'build source ELF fixture', 'simulate core/fabric/loader',
                    'synthesize', 'route', 'collect and gate all reports',
                    'package local H3 developer checkpoint'
                ],
                'external_commands_executed':
                False
            },
                       indent=2)
        )
        return
    require(args.hdk_root, '--hdk-root is required')
    hdk = args.hdk_root.resolve()
    if args.inventory:
        print(json.dumps(dependency_inventory(hdk), indent=2, sort_keys=True))
        return
    require(
        args.coral_repo and args.dependency_lock
        and args.dependency_lock_sha256,
        'Provide --coral-repo, --dependency-lock and its reviewed --dependency-lock-sha256'
    )
    coral = args.coral_repo.resolve()
    require(
        re.fullmatch('[0-9a-f]{64}', args.dependency_lock_sha256),
        'Invalid lock hash'
    )
    require(
        sha(args.dependency_lock) == args.dependency_lock_sha256,
        'Dependency-lock hash mismatch'
    )
    validate_coral(coral)
    dependencies = verify_dependency_lock(hdk, args.dependency_lock)
    versions = tool_versions(
        args.bazel,
        args.verilator,
        args.clang,
        args.linker,
        vivado=args.build or args.preflight
    )
    if args.preflight:
        print(
            json.dumps({
                'preflight': 'PASS',
                'versions': versions,
                'dependency_lock_sha256': args.dependency_lock_sha256,
                'build': 'NOT_RUN',
                'hardware_calibration': 'NOT_RUN'
            },
                       indent=2)
        )
        return
    require(args.output, '--output is required')
    output = args.output.absolute()
    require(
        output == output.resolve(),
        'Output aliases or parent traversal are unsupported'
    )
    validate_output(output, coral, hdk)
    require(
        re.fullmatch(r'\d{4}_\d{2}_\d{2}-\d{6}', args.tag), 'Invalid build tag'
    )
    if args.build:
        require(
            shutil.disk_usage(output.parent).free
            >= PINS['minimum_free_gib'] * 1024**3,
            'Insufficient output disk space'
        )
        require(
            Path(os.environ.get('VIVADO_SETTINGS', '')).is_file(),
            'Set VIVADO_SETTINGS'
        )
    output.mkdir(mode=0o700)
    status = {
        'tag': args.tag,
        'status': 'running',
        'hardware_calibration': 'NOT_RUN',
        'physical_inference': 'NOT_RUN',
        'retained_tar_reproduction': 'NOT_PROVEN',
        'pins': PINS,
        'versions': versions,
        'dependency_lock_sha256': args.dependency_lock_sha256
    }
    try:
        if args.build:
            status['vivado_settings_sha256'] = sha(
                Path(os.environ['VIVADO_SETTINGS'])
            )
        before = tree_hashes(CI)
        (output /
         'source-hashes.json').write_text(json.dumps(before, indent=2) + '\n')
        (output / 'dependency-lock.json'
         ).write_text(json.dumps(dependencies, indent=2) + '\n')
        generation = generate(output, coral, hdk, args.tag, args.bazel)
        status['generation'] = generation
        status['fixture'] = build_fixture(
            output / 'tests', args.clang, args.linker
        )
        run([sys.executable,
             str(output / 'tests/test_hbm_loader.py')],
            output / 'loader.log',
            output, ['PASS: HBM ELF parsing'],
            timeout=60)
        if args.generate_only:
            status['status'] = 'generated_only'
            return
        env = dict(
            os.environ,
            CORAL_ROOT=str(output),
            AWS_FPGA_REPO_DIR=str(hdk),
            VERILATOR=args.verilator,
            PYTHONDONTWRITEBYTECODE='1'
        )

        def vendor(command, name, markers=(), cwd=output):
            run(['bash', str(CI / 'vendor_stage.sh'), *command],
                output / (name + '.log'), cwd, markers,
                PINS['stage_timeout_seconds'],
                dict(env, CORAL_STAGE_DIR=str(cwd)))

        def vivado(script, name, markers=(), extra=()):
            vendor([
                'vivado', '-mode', 'batch', '-source',
                str(script), '-log',
                str(output / (name + '-vivado.log')), '-journal',
                str(output / (name + '-vivado.jou')), *extra
            ], name, markers)

        vivado(
            output / 'create_hbm_fabric.tcl', 'fabric-generation',
            ['HBM_FABRIC_GENERATION_COMPLETE']
        )
        vendor(['bash', str(output / 'test_hbm.sh')], 'core', [
            'PASS: HBM bank 0', 'PASS: HBM bank 7', 'PASS: HBM bank isolation'
        ])
        vivado(
            output / 'simulate_fabric.tcl', 'fabric',
            ['PASS: both production SmartConnects, 50/250/300MHz']
        )
        generated = generated_inputs(output)
        (output / 'generated-inputs.json'
         ).write_text(json.dumps(generated, indent=2) + '\n')
        cl = output / 'cl_coralnpu_hbm'
        driver = cl / 'build/scripts/aws_build_dcp_from_cl.py'
        for flow, stage in [('SynthCL', 'synthesis'),
                            ('ImplCL', 'implementation')]:
            vendor([
                sys.executable,
                str(driver), '-c', 'cl_coralnpu_hbm', '--mode', 'small_shell',
                '--no-encrypt', '--aws_clk_gen', '--flow', flow, '--tag',
                args.tag, '--clock_recipe_a', 'A1', '--clock_recipe_b', 'B2',
                '--clock_recipe_c', 'C0', '--clock_recipe_hbm', 'H3'
            ],
                   stage,
                   cwd=driver.parent)
            suffix = 'synth' if flow == 'SynthCL' else 'route'
            require((
                cl / 'build/checkpoints' /
                f'cl_coralnpu_hbm.{args.tag}.post_{suffix}.dcp'
            ).is_file(
            ), f'Missing {stage} product; vendor driver return code is not sufficient'
                    )
        checkpoints = cl / 'build/checkpoints'
        dcp = checkpoints / f'cl_coralnpu_hbm.{args.tag}.post_route.dcp'
        require(
            dcp.is_file() and not list(checkpoints.glob('*.VIOLATED.dcp')),
            'Missing or timing-violated routed checkpoint'
        )
        fingerprint = sha(dcp)
        reports = output / 'reports'
        vivado(
            CI / 'validate.tcl', 'validation',
            ['HBM_REPORT_COLLECTION_COMPLETE'],
            ['-tclargs', str(dcp), str(reports)]
        )
        result = qualify(reports)
        status['qualification'] = result
        require(
            result['qualified'],
            'Qualification failed; see reports/qualification.json'
        )
        require(sha(dcp) == fingerprint, 'DCP changed during validation')
        require(
            tree_hashes(CI) == before,
            'Automation sources changed during build'
        )
        require(
            generated_inputs(output) == generated,
            'Generated design inputs changed during build'
        )
        validate_coral(coral)
        require(
            dependency_inventory(hdk) == dependencies,
            'HDK/IP inputs changed during build'
        )
        shutil.copy2(
            reports / 'debug_probes.ltx',
            checkpoints / f'{args.tag}.debug_probes.ltx'
        )
        vendor([sys.executable,
                str(CI / 'package_checkpoint.py'), args.tag], 'package')
        package = output / f'{args.tag}.Developer_CL.tar'
        shutil.copy2(checkpoints / package.name, package)
        verify_package(package, fingerprint)
        require(sha(dcp) == fingerprint, 'DCP changed during packaging')
        require(
            sha(Path(os.environ['VIVADO_SETTINGS'])
                ) == status['vivado_settings_sha256'],
            'Vendor settings changed during build'
        )
        status.update(
            status='build_qualified',
            dcp_sha256=fingerprint,
            package_sha256=sha(package)
        )
        (output / 'SHA256SUMS').write_text(f'{sha(package)}  {package.name}\n')
    except BaseException as exc:
        status.update(status='failed', error=str(exc))
        raise
    finally:
        (output /
         'result.json').write_text(json.dumps(status, indent=2) + '\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'BUILD FAILED: {exc}', file=sys.stderr)
        sys.exit(1)
