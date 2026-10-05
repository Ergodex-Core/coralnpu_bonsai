# Physical AWS F2 tests

This suite targets the TCM-only CoralNPU image: RV32, FLEN=32, VLEN=128,
ZVE32F enabled; 8 KiB ITCM at `0`, 32 KiB DTCM at `0x10000`, and BAR0
control words at `0x30000`. It uses no DDR, HBM, DMA, or external memory.
It does not create, upload, or load an FPGA image.

Build using **Clang and LLD 18.1.3** and LLVM 18 inspection tools:

```sh
python3 fpga/aws/tests/build_examples.py --source-root . --output /tmp/coral-tests
python3 -m unittest discover -s fpga/aws/tests -p 'test_*.py' -v
```

The builder verifies each example and the original startup/CRT against revision
`382b5c12030ad8eb74ab8deafb2529301faeab16`. The original files remain unchanged.
`start.S` declares the upstream `._init` section executable before including it,
which LLVM's assembler requires. Generated wrappers declare `main` with C linkage
because freestanding C++ does not provide its hosted-language special treatment.
`runtime.cc` provides only memcpy, memset, an empty finalizer, and a trap handler;
these four examples do not require a standard library or constructors.

The explicit ISA excludes compressed and double-precision instructions. Disassembly
checks require scalar multiplication, an indirect call and memcpy, scalar `fadd.s`,
and RVV `vle8.v`, `vwadd.vv`, `vse16.v` respectively. Review the retained disassembly
and ELF attributes when changing compiler flags. The linker allocates a 4 KiB
stack plus a 64-byte guard in DTCM. The ELF reader rejects out-of-range, overlapping,
non-executable-entry, truncated, and incompatible ABI load images. There is no
external-memory linker region.

| Case | Unmodified upstream source | Success condition |
|---|---|---|
| `math` | `tests/cocotb/math.cc` | Internal arithmetic result is 5 and `_ret == 0` |
| `fptr` | `tests/cocotb/fptr.cc` | Indirect memcpy copies `0xdeadbeef` and `_ret == 0` |
| `float_add` | `examples/hello_world_add_floats.cc` | Every float equals the host's exact result; first run `[0..7] + 10 == [10..17]`; `_ret == 0` |
| `rvv_add` | `examples/rvv_add_intrinsic.cc` | Every one of 1024 signed 16-bit outputs is 7; `_ret == 0` |

After an approved image is already loaded, run on its FPGA host with the AWS FPGA
SDK installed. This command resets the selected core and overwrites its TCM;
coordinate exclusive use with other operators first. All suite instances share a
per-slot host lock, but other tools must honor that same operational exclusivity.

```sh
sudo -E python3 fpga/aws/tests/run_physical.py \
  --bundle /tmp/coral-tests \
  --expected-agfi "${APPROVED_AGFI:?set the approved loaded image ID}" \
  --expected-shell 0x10212415 --slot 0 --repeat 3 \
  --report /tmp/coral-physical-results.json
```

Preflight reads `fpga-describe-local-image -S 0` without PCI rescan and refuses a
cleared slot, different image, or different shell. The harness does not request
cloud credentials. An AGFI identifies the loaded image; retain the separate
approved artifact-to-AGFI promotion record with results.

Each repetition resets the core, requires cleared halt/fault state, reloads and
reads back every ELF byte including BSS, poisons outputs and `_ret`, checks the
start PC, releases reset, and waits for halt with no fault. `_ret` must change from
`0x0badd00d` to zero; a halt alone is never success. Float inputs change each
repetition, every output is checked, and the stack guard must survive. The original
CRT clears BSS and initializes its own sentinel before calling main. The observed
stack pattern measures a lower bound on stack use; it is not a static proof of
maximum stack depth. Reset remains asserted when the suite exits if access works.

SDK calls run in a separate process with a 2-second response watchdog, reset and
execution have configurable bounded timeouts, and load/readback and final
validation stages each have a 60-second deadline. A stuck SDK worker is terminated;
if access fails, cleanup may be unable to reset the core and the report fails.

Build outputs include every ELF, map, disassembly, ELF attributes, and a JSON
manifest with source hashes, compiler hash/version, exact flags, segment geometry,
entry point and symbols. The hardware report includes that manifest, loaded-image
status, expected and actual values, elapsed times, and cleanup outcome. A failed
preflight still writes a failed report. Keep reports outside source control when
they contain operational paths or loaded-image identifiers.

`test_harness.py` uses synthetic memory and fault injection to test host logic. Its
passing result proves neither RISC-V execution nor RTL simulation nor hardware
execution. Build manifests are marked `build_only`; hardware reports are marked
`physical_fpga` and only report `passed` after all physical checks complete.
