#!/usr/bin/env python3
"""Lint production top ports against pinned HDK and emitted core declarations.

This is an interface-only check: the shell DDR, clock converter, clock buffer,
and core are black boxes. It does not qualify the DDR controller or FPGA build.
No local HDK checkout changes are made; files are read with git show at pins.json.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
AWS = HERE.parents[1]
DESIGN = AWS / 'cl_coralnpu' / 'design'


def clock_stubs():
    fields = [('awaddr', 32, True), ('awprot', 3, True), ('awvalid', 1, True),
              ('awready', 1, False), ('wdata', 32, True), ('wstrb', 4, True),
              ('wvalid', 1, True), ('wready', 1, False), ('bresp', 2, False),
              ('bvalid', 1, False), ('bready', 1, True), ('araddr', 32, True),
              ('arprot', 3, True), ('arvalid', 1, True), ('arready', 1, False),
              ('rdata', 32, False), ('rresp', 2, False), ('rvalid', 1, False),
              ('rready', 1, True)]
    ports = ['input s_axi_aclk,s_axi_aresetn,m_axi_aclk,m_axi_aresetn']
    for prefix in ('s', 'm'):
        for name, width, master_output in fields:
            is_input = master_output if prefix == 's' else not master_output
            ports.append(('input ' if is_input else 'output ') +
                         (f'[{width-1}:0] ' if width > 1 else '') +
                         f'{prefix}_axi_{name}')
    return (
        'module cl_axi_clock_converter_light(\n' + ',\n'.join(ports) +
        '\n); endmodule\n'
        'module BUFGCE_DIV #(parameter BUFGCE_DIVIDE=1, SIM_DEVICE="ULTRASCALE_PLUS")'
        '(input I,CE,CLR,output O); endmodule\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hdk-repo', type=Path, required=True)
    parser.add_argument('--core-rtl', type=Path, required=True)
    args = parser.parse_args()
    pins = json.loads((AWS / 'build' / 'pins.json').read_text())
    core_bytes = args.core_rtl.read_bytes()
    if hashlib.sha256(core_bytes
                      ).hexdigest() != pins['reference_emitted_rtl_sha256']:
        raise SystemExit(
            'Generated core RTL does not match pinned reference hash'
        )
    core = core_bytes.decode()
    start = core.index('module RvvCoreMiniAxi(')
    end = core.index(');', start) + 2
    with tempfile.TemporaryDirectory(prefix='coral-ddr-interface-') as name:
        tmp = Path(name)
        for relative, target in (
            ('hdk/common/shell_stable/design/interfaces/cl_ports.vh',
             'cl_ports.vh'),
            ('hdk/common/shell_stable/design/sh_ddr/sh_ddr.stub.sv',
             'sh_ddr.stub.sv'),
        ):
            data = subprocess.check_output([
                'git', '-C',
                str(args.hdk_repo), 'show', f"{pins['hdk_commit']}:{relative}"
            ])
            (tmp / target).write_bytes(data)
        (tmp / 'core.stub.sv').write_text(core[start:end] + '\nendmodule\n')
        (tmp / 'clock.stub.sv').write_text(clock_stubs())
        command = [
            'verilator', '--lint-only', '--timing', '--top-module',
            'cl_coralnpu', '-I' + str(tmp), '-I' + str(DESIGN),
            '-Werror-WIDTH', '-Werror-PINMISSING', '-Werror-IMPLICIT',
            '-Werror-MULTIDRIVEN'
        ]
        command += [str(path) for path in sorted(DESIGN.glob('*.sv'))]
        command += [
            str(tmp / name)
            for name in ('sh_ddr.stub.sv', 'core.stub.sv', 'clock.stub.sv')
        ]
        command += [str(HERE / 'xpm_fifo_async_model.sv')]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit(result.stdout + result.stderr)
        print(
            'PASS: production top ports match pinned HDK shell/SH_DDR and core RTL declarations'
        )
        print(
            'Interface-only black-box lint; vendor simulation, CDC timing, synthesis and hardware NOT RUN'
        )


if __name__ == '__main__':
    main()
