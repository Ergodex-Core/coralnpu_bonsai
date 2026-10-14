# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Non-hermetic repository exposing a locally installed ArcSim toolchain.

ArcSim is a licensed, Linux x86-64 only RTL simulator built on CIRCT's
arcilator. It is not downloadable, so this rule wraps whatever install the
developer has. The install location is taken from `ARCSIM_HOME`, falling back
to the extracted matrix bundle at the workspace root, then to
~/.cache/arcsim/<bundle> (where utils/arcsim/setup_arcsim.sh puts it). Two
layouts are recognized:

  * Developer bundle (e.g. arcsim-coral-matrix-baseline-20261013/):
      toolchain/bin/{circt-verilog,arcsim,opt,llc}, lib/*.a, lib/runtime.o
  * Release package (arcsim_*.deb, ARCSIM_HOME=/usr/lib/ergodex):
      bin/{circt-verilog,arcsim,opt,llc}, lib/*.a (includes license libs)

When no usable toolchain is found (including on non Linux x86-64 hosts) the
repository still loads, but its tools are stubs that fail with an explanation.
All consumers are tagged "arcsim" and filtered out of default builds.
"""

DEFAULT_BUNDLE_DIR = "arcsim-coral-matrix-baseline-20261013"

_TOOLS = ["circt-verilog", "arcsim", "opt", "llc"]

# Static link order for the runtime archives. Anything else found in lib/ is
# appended; everything is wrapped in --start-group/--end-group anyway.
_LIB_ORDER = [
    "runtime.o",
    "libCIRCTArcRuntime.a",
    "libCIRCTSupport.a",
    "libErgodexLicense.a",
    "libLLVMSupport.a",
    "libLLVMDemangle.a",
    "libcurl.a",
    "libssl.a",
    "libcrypto.a",
]

# Profiling runtime shipped with the PGO benchmark bundle; never wanted here.
_LIB_SKIP = ["libclang_rt.profile-x86_64.a"]

_SYSTEM_LINKOPTS = [
    "-latomic",
    "-lz",
    "-lzstd",
    "-ltinfo",
    "-pthread",
    "-ldl",
    "-lm",
    # The packaged runtime archives are not guaranteed to be PIC.
    "-no-pie",
]

_STUB_TOOL = """#!/bin/sh
echo "ArcSim is not available: {reason}" >&2
echo "See doc/arcsim.md. Set ARCSIM_HOME to an ArcSim bundle or /usr/lib/ergodex." >&2
exit 1
"""

def _host_supported(rctx):
    os_name = rctx.os.name.lower()
    arch = rctx.os.arch.lower()
    return os_name.startswith("linux") and arch in ["amd64", "x86_64"]

def _detect(rctx):
    """Returns (home, bin_dir, lib_dir, layout) or a string with the reason."""
    home_env = rctx.getenv("ARCSIM_HOME", "")
    if home_env:
        home = rctx.path(home_env)
        if not home.exists:
            return "ARCSIM_HOME ({}) does not exist".format(home)
    else:
        # Prefer a bundle extracted into this checkout, then the shared cache
        # populated by utils/arcsim/setup_arcsim.sh (works across worktrees).
        candidates = [rctx.workspace_root.get_child(DEFAULT_BUNDLE_DIR)]
        user_home = rctx.getenv("HOME", "")
        if user_home:
            candidates.append(rctx.path(user_home).get_child(".cache").get_child("arcsim").get_child(DEFAULT_BUNDLE_DIR))
        existing = [c for c in candidates if c.exists]
        if not existing:
            return "ARCSIM_HOME is unset and no bundle found at {}".format(
                " or ".join([str(c) for c in candidates]),
            )
        home = existing[0]
    if home.get_child("toolchain").get_child("bin").get_child("arcsim").exists:
        return (home, home.get_child("toolchain").get_child("bin"), home.get_child("lib"), "bundle")
    if home.get_child("bin").get_child("arcsim").exists:
        return (home, home.get_child("bin"), home.get_child("lib"), "release")
    return "no toolchain/bin/arcsim or bin/arcsim under {}".format(home)

def _arcsim_repo_impl(rctx):
    found = _detect(rctx)
    if type(found) == "string":
        reason = found
    elif not _host_supported(rctx):
        reason = "ArcSim requires a Linux x86-64 host (this host is {} {})".format(rctx.os.name, rctx.os.arch)
    else:
        reason = None

    if reason:
        for tool in _TOOLS:
            rctx.file("bin/" + tool, _STUB_TOOL.format(reason = reason), executable = True)
        rctx.file("defs.bzl", "ARCSIM_AVAILABLE = False\nARCSIM_HOME = \"\"\nARCSIM_LAYOUT = \"\"\nARCSIM_REASON = {}\n".format(repr(reason)))
        rctx.file("BUILD.bazel", """package(default_visibility = ["//visibility:public"])

exports_files(glob(["bin/*"]))

cc_library(name = "runtime")
""")
        return

    home, bin_dir, lib_dir, layout = found
    for tool in _TOOLS:
        src = bin_dir.get_child(tool)
        if not src.exists:
            fail("ArcSim {} install at {} is missing {}".format(layout, home, src))
        rctx.symlink(src, "bin/" + tool)

    names = [p.basename for p in lib_dir.readdir()] if lib_dir.exists else []
    libs = [n for n in _LIB_ORDER if n in names]
    libs += sorted([
        n
        for n in names
        if (n.endswith(".a") or n.endswith(".o")) and n not in libs and n not in _LIB_SKIP
    ])
    if "libCIRCTArcRuntime.a" not in libs:
        fail("ArcSim {} install at {} has no lib/libCIRCTArcRuntime.a".format(layout, home))

    lib_paths = [str(lib_dir.get_child(n)) for n in libs]
    linkopts = ["-Wl,--start-group"] + lib_paths + ["-Wl,--end-group"] + _SYSTEM_LINKOPTS
    defines = _runtime_defines(rctx, lib_paths)

    rctx.file("defs.bzl", "ARCSIM_AVAILABLE = True\nARCSIM_HOME = {}\nARCSIM_LAYOUT = {}\nARCSIM_REASON = \"\"\n".format(
        repr(str(home)),
        repr(layout),
    ))
    rctx.file("BUILD.bazel", """package(default_visibility = ["//visibility:public"])

# ArcSim {layout} toolchain from {home}
exports_files(glob(["bin/*"]))

cc_library(
    name = "runtime",
    defines = {defines},
    linkopts = {linkopts},
)
""".format(layout = layout, home = home, defines = repr(defines), linkopts = repr(linkopts)))

# Optional runtime entry points. Runtime builds differ between bundles and
# releases, so the harness only references the ones that are present.
_OPTIONAL_SYMBOLS = {
    "arcRuntimeIR_simStepOnce": "ARCSIM_HAS_STEP_ONCE",
    "arcRuntimeIR_simStepSettled": "ARCSIM_HAS_STEP_SETTLED",
    "__circt_sim_now_fs": "ARCSIM_HAS_SIM_NOW",
    "circt_sv_set_plusargs": "ARCSIM_HAS_PLUSARGS",
}

def _runtime_defines(rctx, lib_paths):
    nm = rctx.which("nm")
    if nm == None:
        # buildifier: disable=print
        print("WARNING: nm not found; ArcSim harness will call the model eval directly.")
        return []
    defined = {}
    for path in lib_paths:
        result = rctx.execute([nm, "-g", "--defined-only", path])
        if result.return_code != 0:
            continue
        for line in result.stdout.splitlines():
            fields = line.split(" ")
            if len(fields) >= 3 and fields[-1] in _OPTIONAL_SYMBOLS:
                defined[fields[-1]] = True
    return sorted([_OPTIONAL_SYMBOLS[s] + "=1" for s in defined])

arcsim_repo = repository_rule(
    implementation = _arcsim_repo_impl,
    local = True,
    environ = ["ARCSIM_HOME", "HOME"],
    doc = "Wraps a locally installed ArcSim toolchain (see doc/arcsim.md).",
)
