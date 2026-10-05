#!/usr/bin/env python3
"""CI for an already generated Coral HBM CL; no RTL/CL generation."""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def check_log(path, markers=()):
    text = Path(path).read_text(errors='replace')
    require(not re.search(r'^(?:ERROR:|FATAL:|%Error)', text, re.M), f'Error in {path}')
    for marker in markers:
        require(marker in text, f'Missing success marker {marker!r} in {path}')


def run(command, log, cwd, markers=()):
    print(f'Running {log.stem}; log: {log}', flush=True)
    with log.open('w') as f:
        result = subprocess.run(command, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    require(result.returncode == 0, f'Command failed ({result.returncode}); see {log}')
    check_log(log, markers)


def source_hashes(root):
    files = []
    for folder in ('cl_coralnpu_hbm/design', 'cl_coralnpu_hbm/rtl',
                   'cl_coralnpu_hbm/build/scripts', 'cl_coralnpu_hbm/build/constraints', 'tests', 'ci'):
        files.extend(p for p in (root/folder).rglob('*') if p.is_file()
                     and p.suffix in {'.sv', '.v', '.vh', '.svh', '.tcl', '.xdc', '.py', '.sh', '.S', '.elf', '.ld'})
    files.extend(root/n for n in ('hbm_300mhz.tcl', 'test_hbm.sh', 'simulate_fabric.tcl', 'hbm_host.py') if (root/n).exists())
    files.extend(p for p in (root/'ip/fabric').rglob('*') if p.is_file() and p.suffix in {'.bd', '.xci', '.v', '.sv'})
    return {str(p.relative_to(root)): sha(p) for p in sorted(set(files))}


def aws_json(args):
    return json.loads(subprocess.check_output(['aws', *args, '--output', 'json'], text=True))


def publish(package, out, args, tag):
    prefix = f'{args.prefix.strip("/")}/{tag}'
    key = f'{prefix}/{package.name}'
    subprocess.run(['aws', 's3', 'cp', str(package), f's3://{args.bucket}/{key}',
                    '--region', args.region, '--only-show-errors'], check=True)
    (out/'upload.json').write_text(json.dumps(dict(bucket=args.bucket, key=key, region=args.region), indent=2)+'\n')
    if not args.create_afi:
        return
    # Deterministic client token permits recovery of a timed-out create request.
    token = hashlib.sha256(f'{args.bucket}/{key}/{sha(package)}'.encode()).hexdigest()
    command = ['ec2', 'create-fpga-image', '--region', args.region,
               '--name', f'coralnpu-hbm-{tag}', '--client-token', token,
               '--input-storage-location', json.dumps(dict(Bucket=args.bucket, Key=key)),
               '--logs-storage-location', json.dumps(dict(Bucket=args.bucket, Key=f'{prefix}/afi-logs'))]
    # Save the exact request before contacting AWS for recovery after interruptions.
    (out/'afi-request.json').write_text(json.dumps(command, indent=2)+'\n')
    image = aws_json(command)
    (out/'afi.json').write_text(json.dumps(image, indent=2)+'\n')
    deadline = time.monotonic()+args.afi_timeout
    while time.monotonic() < deadline:
        state = aws_json(['ec2', 'describe-fpga-images', '--region', args.region,
                          '--fpga-image-ids', image['FpgaImageId']])
        (out/'afi-state.json').write_text(json.dumps(state, indent=2)+'\n')
        code = state['FpgaImages'][0]['State']['Code']
        print(f'AFI {image["FpgaImageId"]}: {code}', flush=True)
        if code == 'available':
            return
        require(code == 'pending', f'AFI failed: {state}')
        time.sleep(30)
    raise TimeoutError('AFI still pending; IDs saved in afi.json. Do not create a replacement.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true', help='Check prerequisites only; no build or AWS calls')
    parser.add_argument('--generate-cl', action='store_true', help='Generate Coral RTL and assemble the fixed HBM CL before building')
    parser.add_argument('--generate-only', action='store_true', help='Generate CL only; never launch Vivado or publish')
    parser.add_argument('--coral-repo', required=True, help='Explicit Coral source checkout')
    parser.add_argument('--bazel', default='bazel')
    parser.add_argument('--rtl-dir', help='Use existing RvvCoreMiniAxi.sv/.zip instead of running Bazel')
    parser.add_argument('--tag', default=dt.datetime.now(dt.timezone.utc).strftime('%Y_%m_%d-%H%M%S'))
    parser.add_argument('--bucket', help='Explicit opt-in to uploading the validated package')
    parser.add_argument('--prefix', default='coralnpu-hbm/ci')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--create-afi', action='store_true', help='Requires --bucket; create and wait for AFI, without loading hardware')
    parser.add_argument('--afi-timeout', type=int, default=7200)
    args = parser.parse_args()
    require(re.fullmatch(r'\d{4}_\d{2}_\d{2}-\d{6}', args.tag), 'Tag must be YYYY_MM_DD-HHMMSS')
    require(not args.create_afi or args.bucket, '--create-afi requires --bucket')
    require(args.afi_timeout > 0, 'AFI timeout must be positive')
    require(not args.generate_only or not args.bucket, '--generate-only cannot publish')
    args.generate_cl = args.generate_cl or args.generate_only
    root = Path(os.environ['CORAL_ROOT']).resolve()
    cl = root/'cl_coralnpu_hbm'
    hdk = Path(os.environ['AWS_FPGA_REPO_DIR']).resolve()
    hdk_commit = subprocess.check_output(['git', '-C', str(hdk), 'rev-parse', 'HEAD'], text=True).strip()
    require(hdk_commit == '20fe90180574464fd509c994680ec67191e9759f',
            'HDK revision changed: review clock/CDC exceptions and update the pinned revision before CI')
    # The pinned HDK packager interpolates these paths into shell commands.
    for path in (root, hdk):
        require(re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)), 'HDK requires simple absolute paths without spaces or shell metacharacters')
    lock = (root/'.coral-ci.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another CI run is using this workspace')
    generation = None
    if args.generate_cl:
        from generate_cl import check_inputs, generate
        check_inputs(args.coral_repo, args.bazel, args.rtl_dir)
        if not args.preflight:
            generation = generate(root, args.coral_repo, hdk, args.tag, args.bazel, args.rtl_dir)
            if args.generate_only:
                print('Generation complete. Vivado, SPNR, S3 and AFI stages were not run.')
                lock.close()
                return
    driver = cl/'build/scripts/aws_build_dcp_from_cl.py'
    for p in (driver, root/'hbm_300mhz.tcl', root/'test_hbm.sh', root/'simulate_fabric.tcl',
              root/'tests/test_hbm_loader.py', Path(__file__).with_name('validate.tcl')):
        require(p.is_file(), f'Missing prerequisite: {p}')
    require('hbm_300mhz.tcl' in (cl/'build/scripts/build_level_1_cl.tcl').read_text(),
            'Implementation must apply hbm_300mhz.tcl after shell linking')
    for exe in ('vivado', 'bash', 'python3') + (('aws',) if args.bucket else ()):
        require(shutil.which(exe), f'Missing executable: {exe}')
    verilator = os.environ.get('VERILATOR', 'verilator')
    require(shutil.which(verilator), f'Missing Verilator: {verilator}')
    os.environ['VERILATOR'] = verilator
    require(shutil.disk_usage(root).free > 30*1024**3, 'At least 30 GiB free workspace space required')
    if args.preflight:
        print('Preflight passed. No synthesis, upload, or AFI creation performed.')
        return
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another CI run is using this CL; use a separate workspace')
        out = root/'ci-runs'/args.tag
        out.mkdir(parents=True, exist_ok=False)
        status = dict(tag=args.tag, status='running', hardware_tested=False, hdk_commit=hdk_commit)
        try:
            cp = cl/'build/checkpoints'
            require(not list(cp.glob(f'*{args.tag}*')), 'Tag already has build products; choose a new tag')
            if generation:
                (out/'generation.json').write_text(json.dumps(generation, indent=2)+'\n')
                # IP generation is part of an actual full pipeline, never --generate-only.
                run(['vivado', '-mode', 'batch', '-source', str(root/'create_hbm_fabric.tcl'),
                     '-log', str(out/'fabric-generation-vivado.log'), '-journal', str(out/'fabric-generation-vivado.jou')],
                    out/'fabric-generation.log', root)
                require((root/'ip/fabric/fabric.srcs/sources_1/bd/coral_hbm_fabric/coral_hbm_fabric.bd').is_file(), 'HBM fabric generation failed')
            before = source_hashes(root)
            (out/'sources.json').write_text(json.dumps(before, indent=2)+'\n')
            run(['bash', 'test_hbm.sh'], out/'core.log', root,
                ['PASS: HBM bank 0', 'PASS: HBM bank 7', 'PASS: HBM bank isolation'])
            run(['vivado', '-mode', 'batch', '-source', str(root/'simulate_fabric.tcl'),
                 '-log', str(out/'fabric-vivado.log'), '-journal', str(out/'fabric-vivado.jou')],
                out/'fabric.log', root, ['PASS: both production SmartConnects, 50/250/300MHz'])
            run(['python3', 'tests/test_hbm_loader.py'], out/'loader.log', root, ['PASS: HBM ELF parsing'])
            for flow, stage in [('SynthCL', 'synthesis'), ('ImplCL', 'implementation')]:
                run(['python3', str(driver), '-c', 'cl_coralnpu_hbm', '--mode', 'small_shell',
                     '--no-encrypt', '--flow', flow, '--tag', args.tag,
                     '--clock_recipe_a', 'A1', '--clock_recipe_b', 'B2',
                     '--clock_recipe_c', 'C0', '--clock_recipe_hbm', 'H2'],
                    out/f'{stage}.log', driver.parent)
                product = cp/f'cl_coralnpu_hbm.{args.tag}.post_{"synth" if flow=="SynthCL" else "route"}.dcp'
                require(product.is_file(), f'Expected checkpoint missing: {product}')
            require(not (cp/f'cl_coralnpu_hbm.{args.tag}.post_route.VIOLATED.dcp').exists(), 'Timing-violated build rejected')
            dcp = product
            fingerprint = sha(dcp)
            reports = out/'reports'; reports.mkdir()
            run(['vivado', '-mode', 'batch', '-source', str(Path(__file__).with_name('validate.tcl')),
                 '-log', str(out/'validation-vivado.log'), '-journal', str(out/'validation-vivado.jou'),
                 '-tclargs', str(dcp), str(reports)], out/'validation.log', out, ['CI_VALIDATION_PASS'])
            require(sha(dcp)==fingerprint, 'Checkpoint changed during validation')
            # Generated clocks XDC is expected to be regenerated by the HDK.
            after = source_hashes(root)
            keep = lambda m: {k:v for k,v in m.items() if not k.endswith('/generated_cl_clocks_aws.xdc')}
            require(keep(before)==keep(after), 'Sources changed during this run; refusing mixed build')
            shutil.copy2(reports/'debug_probes.ltx', cp/f'{args.tag}.debug_probes.ltx')
            spec = importlib.util.spec_from_file_location('hdk_packager', hdk/'hdk/common/shell_stable/build/scripts/aws_build_dcp_from_cl.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            os.chdir(out)
            module.generate_dcp_tarball('cl_coralnpu_hbm', args.tag, 'A1', 'B2', 'C0', 'H2')
            package = out/f'{args.tag}.Developer_CL.tar'
            shutil.copy2(cp/package.name, package)
            with tarfile.open(package) as archive:
                members = [m for m in archive.getmembers() if m.name.endswith('.dcp')]
                require(len(members)==1, 'Expected exactly one DCP in upload package')
                with archive.extractfile(members[0]) as stream:
                    require(hashlib.file_digest(stream, 'sha256').hexdigest()==fingerprint, 'Packaged checkpoint differs from validated checkpoint')
            status.update(status='validated', package=str(package), sha256=sha(package), dcp_sha256=fingerprint,
                          npu_mhz=50, hbm_axi_mhz=300)
            (out/'SHA256SUMS').write_text(f'{status["sha256"]}  {package.name}\n')
            (out/'result.json').write_text(json.dumps(status, indent=2)+'\n')
            if args.bucket:
                publish(package, out, args, args.tag)
                status['status'] = 'afi_available' if args.create_afi else 'uploaded'
            print(f'CI passed: {package}', flush=True)
        except BaseException as exc:
            status.update(status='failed', error=str(exc))
            raise
        finally:
            (out/'result.json').write_text(json.dumps(status, indent=2)+'\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'CI FAILED: {exc}', file=sys.stderr)
        sys.exit(1)
