#!/usr/bin/env python3
"""Generate the fixed Coral RVV128/HBM CL from Bazel RTL and editable templates."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import zipfile

TARGET = '//hdl/chisel/src/coralnpu:rvv_core_mini_axi_cc_library_emit_verilog'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_inputs(repo, bazel, rtl_dir=None):
    repo = Path(repo).resolve()
    if not (repo/'hdl/chisel/src/coralnpu/BUILD').is_file():
        raise RuntimeError(f'Not a Coral NPU checkout: {repo}')
    if rtl_dir:
        for name in ('RvvCoreMiniAxi.sv', 'RvvCoreMiniAxi.zip'):
            if not (Path(rtl_dir)/name).is_file():
                raise RuntimeError(f'Missing generated RTL: {name}')
    elif not shutil.which(bazel):
        raise RuntimeError(f'Bazel executable not found: {bazel}')


def generate(root, repo, hdk, tag, bazel, rtl_dir=None):
    root, repo, hdk = map(lambda p: Path(p).resolve(), (root, repo, hdk))
    check_inputs(repo, bazel, rtl_dir)
    templates = Path(__file__).with_name('templates')
    work = root/'ci-generation'/tag
    work.mkdir(parents=True, exist_ok=False)
    staged = work/'project'; staged.mkdir()
    used_pre_generated = rtl_dir is not None
    with (work/'generation.log').open('w') as log:
        if rtl_dir is None:
            subprocess.run([bazel, 'build', TARGET], cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True)
            bazel_bin = subprocess.check_output([bazel, 'info', 'bazel-bin'], cwd=repo, stderr=log, text=True).strip()
            rtl_dir = Path(bazel_bin)/'hdl/chisel/src/coralnpu'
        rtl_dir = Path(rtl_dir).resolve()
        sv, archive = rtl_dir/'RvvCoreMiniAxi.sv', rtl_dir/'RvvCoreMiniAxi.zip'
        original = sv.read_text()
        if not re.search(r'\bmodule\s+RvvCoreMiniAxi\b', original):
            raise RuntimeError('Generator did not produce RvvCoreMiniAxi')
        # AWS defines its own sync module; rename the Coral module and instances.
        rename = lambda text: re.sub(r'\bsync\b', 'coral_private_sync', text)
        cl = staged/'cl_coralnpu_hbm'
        for folder in ('build/checkpoints','build/reports','build/src_post_encryption'):
            (cl/folder).mkdir(parents=True, exist_ok=True)
        include = cl/'rtl/include'; include.mkdir(parents=True)
        (cl/'rtl/RvvCoreMiniAxi.sv').write_text(rename(original))
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                path = PurePosixPath(item.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in item.filename:
                    raise RuntimeError(f'Unsafe RTL archive entry: {item.filename}')
                if item.is_dir():
                    continue
                dst = include/str(path); dst.parent.mkdir(parents=True, exist_ok=True)
                data = z.read(item)
                if path.suffix in {'.sv','.v','.vh','.svh'}:
                    data = rename(data.decode()).encode()
                dst.write_bytes(data)
        template_hashes = {}
        for src in sorted(templates.rglob('*')):
            if not src.is_file() or src.name == 'hdk-links.json':
                continue
            rel = src.relative_to(templates)
            dst = staged/rel; dst.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes().replace(b'@@CORAL_ROOT@@',str(root).encode()).replace(b'@@HDK_ROOT@@',str(hdk).encode())
            dst.write_bytes(data)
            template_hashes[str(rel)] = digest(src)
        links = json.loads((templates/'hdk-links.json').read_text())
        for rel, target in links.items():
            source = hdk/target
            if not source.is_file():
                raise RuntimeError(f'Missing HDK dependency: {source}')
            dst = staged/rel; dst.parent.mkdir(parents=True, exist_ok=True); dst.symlink_to(source)
        # Validate include coverage before replacing the prior CL.
        for name in re.findall(r'^\s*`include\s+"([^"]+)"', (cl/'rtl/RvvCoreMiniAxi.sv').read_text(), re.M):
            if not (include/name).is_file():
                raise RuntimeError(f'Missing generated include: {name}')
        commit = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        status = subprocess.check_output(['git','-C',str(repo),'status','--porcelain'],text=True)
        receipt = dict(tag=tag, coral_repo=str(repo), coral_commit=commit, coral_worktree_status=status,
                       bazel_target=TARGET, generated_sv_sha256=digest(sv), generated_zip_sha256=digest(archive),
                       cl_rtl_sha256=digest(cl/'rtl/RvvCoreMiniAxi.sv'), template_sha256=template_hashes,
                       rtl_source='pre-generated override' if used_pre_generated else 'Bazel',
                       vivado_run=False)
        (work/'generation.json').write_text(json.dumps(receipt,indent=2)+'\n')
        backup = root/'ci-cl-backups'/tag
        backup.mkdir(parents=True, exist_ok=False)
        moved, installed = [], []
        try:
            for src in sorted(staged.iterdir()):
                dst = root/src.name
                if dst.exists() or dst.is_symlink():
                    dst.rename(backup/src.name); moved.append(src.name)
                src.rename(dst); installed.append(src.name)
        except BaseException:
            for name in reversed(installed):
                (root/name).rename(staged/name)
            for name in reversed(moved):
                (backup/name).rename(root/name)
            raise
        print(f'CL generated: {root/"cl_coralnpu_hbm"}; previous files: {backup}', flush=True)
        return receipt
