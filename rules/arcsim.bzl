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

"""Bazel rules for ArcSim cycle-accurate simulation models.

ArcSim compiles SystemVerilog through CIRCT (circt-verilog -> arcilator ->
LLVM) into a native model object. `arcsim_model` runs that pipeline and
returns a CcInfo that a plain C++ harness can link against, together with a
generated header describing the model's state layout (see
utils/arcsim/gen_ports.py).

Like the VCS rules, these actions use a locally installed, licensed toolchain
(the `@arcsim` repository, see rules/arcsim_repo.bzl) and run unsandboxed.
Every target is tagged "arcsim" and only builds with `--config=arcsim`.
"""

load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

_TRIPLE = "x86_64-unknown-linux-gnu"

def verilator_vopts_to_arcsim_frontend_args(vopts):
    """Keeps the -D options from a Verilator vopts list.

    Verilator-only flags such as -Wno-* are dropped. This lets arcsim targets
    reuse the vopts lists in hdl/chisel/src/coralnpu/flags.bzl so the two
    simulators always see the same defines. -I options are dropped too:
    Verilator ignores missing include directories but circt-verilog rejects
    them, and the shared lists name directories that do not exist in this
    tree. Pass real include directories through frontend_args/hdrs instead.
    """
    return [o for o in vopts if o.startswith("-D")]

def _arcsim_model_impl(ctx):
    top = ctx.attr.top
    assertions = ctx.attr._assertions[BuildSettingInfo].value
    cpu = ctx.attr.cpu

    sv_files = [f for f in ctx.files.srcs if f.extension in ["sv", "v", "svh"]]
    if not sv_files:
        fail("arcsim_model {} has no SystemVerilog sources".format(ctx.label))
    top_sources = [f for f in sv_files if f.extension != "svh"]

    def out(suffix):
        return ctx.actions.declare_file("{}_arcsim/{}".format(ctx.label.name, suffix))

    design_mlir = out("design.mlir")
    model_ll = out("model.ll")
    state_json = out("state.json")
    model_bc = out("model.opt.bc")
    model_obj = out("model.o")
    # Each simulator links exactly one model, so a fixed header name on a
    # per-target include path keeps the harness source model-independent.
    header = out("arcsim_model.h")

    common_exec = {"no-sandbox": "1", "requires-arcsim": "1"}

    # 1. SystemVerilog -> HW MLIR.
    frontend = ctx.actions.args()
    frontend.add_all([
        "--ir-hw",
        "--single-unit",
        "--timescale=1ps/1ps",
        "--top=" + top,
        "-Wno-mixed-var-assigns",
        "-Wno-index-oob",
    ])
    if not assertions:
        frontend.add_all(["--ignore-assertions", "-DSYNTHESIS"])
    frontend.add_all(ctx.attr.frontend_args)
    frontend.add_all(top_sources)
    frontend.add("-o", design_mlir)
    ctx.actions.run(
        executable = ctx.file._circt_verilog,
        arguments = [frontend],
        inputs = depset(sv_files + ctx.files.hdrs),
        outputs = [design_mlir],
        mnemonic = "ArcsimFrontend",
        progress_message = "[ArcSim] Importing %s (top %s)" % (ctx.label, top),
        execution_requirements = common_exec,
        use_default_shell_env = True,
    )

    # 2. HW MLIR -> arc model -> LLVM IR, plus the state layout.
    lower = ctx.actions.args()
    lower.add(design_mlir)
    lower.add("--emit-llvm")
    lower.add("--state-file=" + state_json.path)
    lower.add_all(ctx.attr.arc_args)
    lower.add("-o", model_ll)
    ctx.actions.run(
        executable = ctx.file._arcsim,
        arguments = [lower],
        inputs = [design_mlir],
        outputs = [model_ll, state_json],
        mnemonic = "ArcsimLower",
        progress_message = "[ArcSim] Lowering %s" % ctx.label,
        execution_requirements = common_exec,
        use_default_shell_env = True,
    )

    # 3. Optimize and generate native code.
    target = ["-mtriple=" + _TRIPLE, "-mcpu=" + cpu]
    opt = ctx.actions.args()
    opt.add_all(target)
    opt.add_all(["-O3", "-vectorize-slp=true"])
    opt.add_all(ctx.attr.opt_args)
    opt.add(model_ll)
    opt.add("-o", model_bc)
    ctx.actions.run(
        executable = ctx.file._opt,
        arguments = [opt],
        inputs = [model_ll],
        outputs = [model_bc],
        mnemonic = "ArcsimOpt",
        progress_message = "[ArcSim] Optimizing %s" % ctx.label,
        execution_requirements = common_exec,
        use_default_shell_env = True,
    )

    llc = ctx.actions.args()
    llc.add_all(target)
    llc.add_all(["-O3", "-filetype=obj", "-relocation-model=pic"])
    llc.add(model_bc)
    llc.add("-o", model_obj)
    ctx.actions.run(
        executable = ctx.file._llc,
        arguments = [llc],
        inputs = [model_bc],
        outputs = [model_obj],
        mnemonic = "ArcsimCodegen",
        progress_message = "[ArcSim] Generating native code for %s" % ctx.label,
        execution_requirements = common_exec,
        use_default_shell_env = True,
    )

    # 4. Model header from the state layout of *this* build.
    gen = ctx.actions.args()
    gen.add("--state", state_json)
    gen.add("--top", top)
    gen.add("--out", header)
    ctx.actions.run(
        executable = ctx.executable._gen_ports,
        arguments = [gen],
        inputs = [state_json],
        outputs = [header],
        mnemonic = "ArcsimPorts",
        progress_message = "[ArcSim] Generating model header for %s" % ctx.label,
    )

    compilation_context = cc_common.create_compilation_context(
        headers = depset([header]),
        quote_includes = depset([header.dirname]),
    )
    linker_input = cc_common.create_linker_input(
        owner = ctx.label,
        additional_inputs = depset([model_obj]),
        # Link the object directly so it is never dropped as an unused member.
        user_link_flags = depset([model_obj.path]),
    )
    model_cc_info = CcInfo(
        compilation_context = compilation_context,
        linking_context = cc_common.create_linking_context(
            linker_inputs = depset([linker_input]),
        ),
    )

    return [
        DefaultInfo(files = depset([header, model_obj, state_json])),
        OutputGroupInfo(
            mlir = depset([design_mlir]),
            llvm = depset([model_ll, model_bc]),
            state = depset([state_json]),
        ),
        cc_common.merge_cc_infos(cc_infos = [model_cc_info, ctx.attr._runtime[CcInfo]]),
    ]

_arcsim_model = rule(
    implementation = _arcsim_model_impl,
    doc = "Compiles SystemVerilog into a linkable ArcSim model (CcInfo).",
    attrs = {
        "srcs": attr.label_list(
            allow_files = [".sv", ".v", ".svh"],
            mandatory = True,
            doc = "SystemVerilog sources. Files ending in .svh are inputs only.",
        ),
        "hdrs": attr.label_list(
            allow_files = True,
            doc = "Extra include files needed by the frontend (-I directories).",
        ),
        "top": attr.string(mandatory = True, doc = "Top module name."),
        "frontend_args": attr.string_list(
            doc = "Extra circt-verilog arguments, e.g. -D/-I options.",
        ),
        "arc_args": attr.string_list(
            doc = "Extra arcsim lowering arguments (e.g. experimental --arc-* passes).",
        ),
        "opt_args": attr.string_list(doc = "Extra LLVM opt arguments."),
        "cpu": attr.string(
            default = "x86-64",
            doc = "Target CPU for the model. 'native' is faster but not portable.",
        ),
        "_assertions": attr.label(default = Label("//rules:arcsim_assertions")),
        "_circt_verilog": attr.label(
            default = Label("@arcsim//:bin/circt-verilog"),
            allow_single_file = True,
            executable = True,
            cfg = "exec",
        ),
        "_arcsim": attr.label(
            default = Label("@arcsim//:bin/arcsim"),
            allow_single_file = True,
            executable = True,
            cfg = "exec",
        ),
        "_opt": attr.label(
            default = Label("@arcsim//:bin/opt"),
            allow_single_file = True,
            executable = True,
            cfg = "exec",
        ),
        "_llc": attr.label(
            default = Label("@arcsim//:bin/llc"),
            allow_single_file = True,
            executable = True,
            cfg = "exec",
        ),
        "_runtime": attr.label(
            default = Label("@arcsim//:runtime"),
            providers = [CcInfo],
        ),
        "_gen_ports": attr.label(
            default = Label("//utils/arcsim:gen_ports"),
            executable = True,
            cfg = "exec",
        ),
    },
)

def arcsim_model(name, tags = [], **kwargs):
    _arcsim_model(name = name, tags = ["arcsim"] + tags, **kwargs)

def arcsim_sim_binary(name, model, defines = [], deps = [], tags = [], **kwargs):
    """A CoralNPU AXI core simulator binary driven by the shared harness.

    Accepts the same command line as //tests/verilator_sim:*_axi_sim
    (--binary, --cycles, ...).
    """
    native.cc_binary(
        name = name,
        srcs = ["@coralnpu_hw//tests/arcsim_sim:arcsim_sim.cc"],
        defines = defines,
        deps = [
            model,
            "@coralnpu_hw//tests/verilator_sim:elf",
            "@coralnpu_hw//tests/verilator_sim:symbol_args",
            "@com_google_absl//absl/flags:flag",
            "@com_google_absl//absl/flags:parse",
            "@com_google_absl//absl/flags:usage",
            "@com_google_absl//absl/log",
            "@com_google_absl//absl/log:check",
            "@com_google_absl//absl/log:initialize",
        ] + deps,
        tags = ["arcsim"] + tags,
        **kwargs
    )
