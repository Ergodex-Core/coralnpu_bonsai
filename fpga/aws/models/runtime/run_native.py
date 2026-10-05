#!/usr/bin/env python3
"""Execute the firmware decoder/generation C sources on the host CPU.

This is numerical qualification ONLY: no FPGA or Coral execution is claimed.
The model is mmap-backed, the C runtime is single-threaded, and host code only
loads inputs, captures diagnostics, compares references and presents results.
"""
import argparse
import ctypes as C
import hashlib
import json
import mmap
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
from test_runtime import State, TRACE, GenerationResult

HERE = Path(__file__).resolve().parent
STAGES = {
    1: 'attn_norm',
    2: 'q',
    3: 'k',
    4: 'v',
    5: 'attention',
    6: 'ffn_norm',
    7: 'gate',
    8: 'up',
    9: 'layer',
    10: 'logits'
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while data := f.read(4 * 1024 * 1024):
            h.update(data)
    return h.hexdigest()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path)
    p.add_argument('--tokens', required=True)
    p.add_argument('--max-new-tokens', type=int, default=4)
    p.add_argument('--eos', default='151645,151643')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument(
        '--compiler',
        '--clang',
        dest='compiler',
        default='cc',
        help='native C compiler (GCC or Clang); never a target firmware build'
    )
    p.add_argument('--trace', action='store_true')
    a = p.parse_args(argv)
    if sys.byteorder != 'little':
        raise ValueError(
            'native trace capture currently requires a little-endian host'
        )
    begin = time.monotonic()
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / 'report.json'
    if report_path.exists():
        raise ValueError(
            'output already contains a report; use a fresh directory'
        )
    manifest = json.loads(a.manifest.read_text())
    segment = manifest['segments'][0]
    model = a.manifest.parent / segment['file']
    package_sha = digest(model)
    if model.stat(
    ).st_size != segment['bytes'] or package_sha != segment['sha256']:
        raise ValueError('package hash/size mismatch')
    sources = [HERE / n for n in ('decoder.c', 'math.c', 'generate.c')]
    recorded_sources = sources + [
        HERE / 'decoder.h', HERE / 'generate.h', HERE / 'math.h',
        HERE.parent / 'model_format.h'
    ]
    source_sha256 = {p.name: digest(p) for p in recorded_sources}
    staged = output / 'sources'
    for path in recorded_sources:
        destination = staged / path.relative_to(HERE.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        if digest(destination) != source_sha256[path.name]:
            raise RuntimeError('source changed while snapshotting')
    library = output / 'decoder.so'
    compiler = shutil.which(a.compiler)
    if compiler is None:
        raise ValueError(f'native compiler not found: {a.compiler}')
    version = subprocess.run([compiler, '--version'],
                             check=True,
                             capture_output=True,
                             text=True).stdout
    flags = [
        '-shared', '-fPIC', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
        '-ffp-contract=off', '-fno-fast-math'
    ]
    if 'clang' in version.lower():
        flags += ['-fno-vectorize', '-fno-slp-vectorize']
    elif 'gcc' in version.lower() or 'Free Software Foundation' in version:
        flags += ['-fno-tree-vectorize', '-fno-tree-slp-vectorize']
    else:
        raise ValueError('native compiler must identify as GCC or Clang')
    staged_sources = [
        staged / path.relative_to(HERE.parent) for path in sources
    ]
    subprocess.run([
        compiler, *flags, *map(str, staged_sources), '-o',
        str(library), '-lm'
    ],
                   check=True,
                   timeout=180)
    lib = C.CDLL(str(library))
    lib.cm_workspace_bytes.argtypes = [C.c_void_p, C.c_uint32]
    lib.cm_workspace_bytes.restype = C.c_uint32
    lib.cm_init.argtypes = [
        C.POINTER(State), C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32,
        C.c_uint32
    ]
    lib.cm_generate.argtypes = [
        C.POINTER(State),
        C.POINTER(C.c_uint32), C.c_uint32, C.c_uint32,
        C.POINTER(C.c_uint32), C.c_uint32,
        C.POINTER(C.c_uint32), C.c_uint32,
        C.POINTER(C.c_float), C.c_uint32,
        C.POINTER(GenerationResult)
    ]
    tokens = [int(x) for x in a.tokens.split(',')]
    eos = [int(x) for x in a.eos.split(',') if x]
    capacity = len(tokens) + max(0, a.max_new_tokens - 1)
    if not tokens or not 0 <= a.max_new_tokens <= 2048 or capacity > 2048:
        raise ValueError('invalid context/decode request')
    with model.open('rb') as source:
        data = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_COPY)
    mapped = (C.c_ubyte * len(data)).from_buffer(data)
    need = lib.cm_workspace_bytes(mapped, capacity)
    if not need: raise ValueError('invalid workspace request')
    workspace = (C.c_float * (need // 4))()
    state = State()
    rc = lib.cm_init(
        C.byref(state), mapped, len(data), workspace, need, capacity
    )
    if rc: raise ValueError(f'cm_init failed: {rc}')
    vocab = struct.unpack_from('<I', data, 52)[0]
    if any(not 0 <= token < vocab for token in tokens):
        raise ValueError('prompt token outside vocabulary')
    if len(eos) > 32 or any(not 0 <= token < vocab for token in eos):
        raise ValueError('EOS token outside vocabulary or too many EOS tokens')
    prompt = (C.c_uint32 * len(tokens))(*tokens)
    stop = (C.c_uint32 * len(eos))(*eos)
    generated = (C.c_uint32 * max(1, a.max_new_tokens))()
    count = vocab * max(1, a.max_new_tokens)
    logits = (C.c_float * count)()
    result = GenerationResult()
    trace = []
    run_begin = time.monotonic()
    last_log = [run_begin]
    if a.trace: (output / 'trace').mkdir(exist_ok=True)

    def record(stage, layer, position, values, n, context):
        name = STAGES[stage]
        blob = C.string_at(values, n * 4)
        item = dict(
            position=position,
            layer=layer,
            stage=name,
            count=n,
            sha256=hashlib.sha256(blob).hexdigest()
        )
        if a.trace:
            filename = f'trace/{position:04d}-{layer:03d}-{name}.f32'
            (output / filename).write_bytes(blob)
            item['file'] = filename
        trace.append(item)
        now = time.monotonic()
        if stage == 10 or now - last_log[0] > 30:
            print(
                json.dumps(
                    dict(
                        progress_stage=name,
                        layer=layer,
                        position=position,
                        elapsed_seconds=now - run_begin
                    )
                ),
                flush=True
            )
            last_log[0] = now

    trace_errors = []

    def guarded_record(*args):
        # ctypes otherwise prints and swallows callback exceptions, which can
        # leave an incomplete trace looking like a successful run.
        if not trace_errors:
            try:
                record(*args)
            except Exception as error:
                trace_errors.append(f'{type(error).__name__}: {error}')

    callback = TRACE(guarded_record)
    state.trace = callback
    rc = lib.cm_generate(
        C.byref(state), prompt, len(tokens), a.max_new_tokens, stop, len(eos),
        generated, a.max_new_tokens, logits, count, C.byref(result)
    )
    elapsed = time.monotonic() - run_begin
    rows = result.generated_count if a.max_new_tokens else 1
    raw = C.string_at(logits, rows * vocab * 4)
    (output / 'logits.f32').write_bytes(raw)
    generated_ids = list(generated[:result.generated_count])
    steps = []
    for i in range(rows):
        row = logits[i * vocab:(i + 1) * vocab]
        top = sorted(range(vocab), key=row.__getitem__, reverse=True)[:5]
        steps.append(
            dict(
                index=i,
                token=int(top[0]),
                top5=[dict(token=t, logit=row[t]) for t in top],
                logits_sha256=hashlib.sha256(
                    raw[i * vocab * 4:(i + 1) * vocab * 4]
                ).hexdigest()
            )
        )
    report = dict(
        schema=1,
        backend='native host execution of freestanding C decoder',
        model_id=manifest['model_id'],
        package_sha256=package_sha,
        config=manifest['config'],
        input_ids=tokens,
        generated_ids=generated_ids,
        max_new_tokens=a.max_new_tokens,
        eos_ids=eos,
        stop_reason={
            0: 'prefill',
            1: 'length',
            2: 'eos'
        }.get(result.stop_reason, 'invalid'),
        return_code=rc,
        completed_tokens=result.completed_tokens,
        workspace_bytes=need,
        workspace_capacity=capacity,
        elapsed_seconds=elapsed,
        setup_seconds=run_begin - begin,
        logits_file='logits.f32',
        logits_sha256=hashlib.sha256(raw).hexdigest(),
        steps=steps,
        trace=trace,
        trace_errors=trace_errors,
        compiler=version,
        compiler_sha256=digest(compiler),
        flags=flags,
        source_sha256=source_sha256,
        library_sha256=digest(library),
        coral_simulation='NOT_RUN',
        physical='NOT_RUN',
        target_cycle_counters='NOT_APPLICABLE_HOST'
    )
    report['sources_unchanged'] = all(
        digest(p) == source_sha256[p.name] for p in recorded_sources
    )
    report['status'] = (
        'PASS' if rc == 0 and not trace_errors and report['sources_unchanged']
        else 'FAIL'
    )
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(
        json.dumps({
            k: report[k]
            for k in (
                'return_code', 'generated_ids', 'stop_reason',
                'completed_tokens', 'elapsed_seconds'
            )
        }),
        flush=True
    )
    if rc: raise RuntimeError(f'cm_generate failed: {rc}; see report')
    if report['status'] != 'PASS':
        raise RuntimeError(
            'native trace capture or source integrity failed; see report'
        )


if __name__ == '__main__': main()
