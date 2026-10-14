#!/usr/bin/env bash
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
#
# Build a routed F2 checkpoint for a CoralNPU variant from the run-054 rebuild
# package (s3://.../coralNPU-bonsai-DCPs/rebuild). See README.md.
#
#   build_afi.sh <stage> [args]
#
# Stages (each writes $WORK/<stage>.done and is skipped once done):
#   hdk                  clone the pinned aws-fpga HDK + IP, run.py check-deps
#   regen <name> <flags> regenerate core RTL with the package's generator from
#                        $SRC (a coralnpu_bonsai checkout) plus extra generator
#                        flags; name=base must reproduce the frozen RTL exactly
#   package <name>       copy the package with input/core from regen <name>
#   build <name>         run.py build (synthesis + place + route, many hours)
#   audit <name>         run.py audit on the routed checkpoint
#   tar <name>           Developer CL tarball + manifest (after a passing audit)
#
# Environment: WORK (default ~/afi), PACKAGE (default $WORK/package/rebuild),
# SRC (coralnpu_bonsai tree whose hdl/ is used), REV (source revision stamped
# into the image's CSRs; default: the package's), VIVADO_SETTINGS.
set -euo pipefail

WORK=${WORK:-$HOME/afi}
PACKAGE=${PACKAGE:-$WORK/package/rebuild}
HDK=$WORK/aws-fpga
GEN=$WORK/generator
VIVADO_SETTINGS=${VIVADO_SETTINGS:-/opt/Xilinx/2025.2/Vivado/settings64.sh}
stage=${1:?usage: build_afi.sh hdk|regen|package|build|audit ...}
shift
mkdir -p "$WORK"
log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
done_mark() { touch "$WORK/$1.done"; log "done: $1"; }
is_done() { [[ -e $WORK/$1.done ]]; }
# Vivado's and the HDK's setup scripts read unset variables.
vivado_env() { set +u; source "$VIVADO_SETTINGS"; set -u; }

case $stage in
  hdk)
    is_done hdk && exit 0
    rev=$(python3 -c "import json;print(json.load(open('$PACKAGE/hdk_inputs.json'))['aws_hdk_revision'])")
    ip=$(python3 -c "import json;print(json.load(open('$PACKAGE/hdk_inputs.json'))['aws_ip_revision'])")
    [[ -d $HDK ]] || git clone --no-checkout https://github.com/aws/aws-fpga.git "$HDK"
    git -C "$HDK" checkout -q "$rev"
    vivado_env
    (cd "$HDK" && export AWS_FPGA_REPO_DIR=$HDK && set +u && source hdk_setup.sh)
    git -C "$HDK/hdk/common/ip" fetch -q origin "$ip"
    git -C "$HDK/hdk/common/ip" checkout -q "$ip"
    python3 "$PACKAGE/run.py" check-deps --hdk "$HDK"
    done_mark hdk
    ;;

  regen)
    name=${1:?regen <name> [extra generator flags...]}; shift
    is_done "regen_$name" && exit 0
    : "${SRC:?set SRC to the coralnpu_bonsai tree to elaborate}"
    if [[ ! -d $GEN ]]; then
      cp -a "$PACKAGE/generator" "$GEN"
      mkdir -p "$GEN/tools"
      curl -sfL https://github.com/bazelbuild/bazel/releases/download/9.1.0/bazel-9.1.0-linux-x86_64 \
        -o "$GEN/tools/bazel-9.1.0"
      (cd "$GEN" && sha256sum -c bazel.sha256)
      chmod +x "$GEN/tools/bazel-9.1.0"
      # S3 does not keep file modes; the Bazel workspace runs these scripts.
      find "$GEN" -name '*.sh' -exec chmod +x {} +
      # The variant's Chisel sources replace the package snapshot's; the rest of
      # the snapshot (BUILD files, Bazel module, firtool) stays as shipped, so
      # new .scala files are added to the coralnpu library's srcs here.
      snap=$GEN/third_party/coralnpu_bonsai/hdl/chisel/src/coralnpu
      rsync -a --include='*/' --include='*.scala' --exclude='*' \
        "$SRC/hdl/chisel/" "$GEN/third_party/coralnpu_bonsai/hdl/chisel/"
      for f in "$SRC"/hdl/chisel/src/coralnpu/*.scala; do
        b=$(basename "$f")
        grep -q "\"$b\"" "$snap/BUILD" ||
          sed -i "0,/\"DBus2Axi.scala\",/s//\"DBus2Axi.scala\",\n        \"$b\",/" "$snap/BUILD"
      done
    fi
    # The generator stamps SOURCE_REVISION into read-only CSRs (Csr.sv kscm0..4),
    # so the image identifies its source. REV defaults to the package's own
    # revision, which `regen base` needs to reproduce run 054 exactly.
    # The workspace-status script stamps `git rev-parse HEAD`; the snapshot has
    # no .git, so make it fall back to SOURCE_REVISION (run 054 built from git).
    status=$GEN/third_party/coralnpu_bonsai/utils/get_workspace_status.sh
    grep -q SOURCE_REVISION "$status" || sed -i 's#^  git_rev="ffffffffffffffffffffffffffffffffffffffff"#  git_rev=$(cat SOURCE_REVISION 2>/dev/null || echo ffffffffffffffffffffffffffffffffffffffff)#' "$status"
    grep -q SOURCE_REVISION "$status"
    rev=${REV:-$(cat "$PACKAGE/generator/third_party/coralnpu_bonsai/SOURCE_REVISION")}
    if [[ ! -e $GEN/runs/generator_build_$rev/exit_code ]]; then
      echo "$rev" > "$GEN/third_party/coralnpu_bonsai/SOURCE_REVISION"
      # The revision reaches ScmInfo through Bazel's volatile status, which never
      # invalidates cached actions; clean (keeping the download cache) to restamp.
      (cd "$GEN/third_party/coralnpu_bonsai" &&
         "$GEN/tools/bazel-9.1.0" --output_user_root="$GEN/runs/cache/bazel" clean)
      (cd "$GEN" && bash scripts/build_generator.sh "generator_build_$rev")
    fi
    [[ $(cat "$GEN/third_party/coralnpu_bonsai/SOURCE_REVISION") == "$rev" ]]
    cfg=$GEN/configs/$name.json
    python3 - "$GEN/configs/fp_compat.json" "$cfg" "$@" <<'EOF'
import json, sys
base, out, extra = sys.argv[1], sys.argv[2], sys.argv[3:]
c = json.load(open(base))
c["name"] = out.rsplit("/", 1)[1][:-5]
c["generator_flags"] += extra
json.dump(c, open(out, "w"), indent=2)
EOF
    (cd "$GEN" && python3 scripts/elaborate.py --config "$cfg" --output "runs/$name" &&
       python3 scripts/dispatch_variant.py "runs/$name" "runs/${name}_dispatch2")
    if [[ ! -e $GEN/rtl/cl_coralnpu.sv ]]; then
      (cd "$GEN" && python3 scripts/generate_top.py --matrix-rs-output-reg 1)
    fi
    if [[ $name == base ]]; then
      # The flow must reproduce the frozen run-054 inputs before a variant is trusted.
      diff -qr "$GEN/runs/base_dispatch2/rtl" "$PACKAGE/input/core/rtl"
      cmp "$GEN/runs/base_dispatch2/coral_compilation.sv" "$PACKAGE/input/core/coral_compilation.sv"
      cmp "$GEN/rtl/cl_coralnpu.sv" "$PACKAGE/input/rtl/cl_coralnpu.sv"
      log "base regeneration matches the frozen run-054 RTL"
    fi
    done_mark "regen_$name"
    ;;

  package)
    name=${1:?package <name>}
    is_done "package_$name" && exit 0
    dest=$WORK/package_$name/rebuild
    mkdir -p "$(dirname "$dest")"
    cp -a "$PACKAGE" "$dest"
    rm -rf "$dest/input/core"
    mkdir -p "$dest/input/core"
    src=$GEN/runs/${name}_dispatch2
    cp -a "$src/rtl" "$src/coral_compilation.sv" "$src/manifest.json" "$src/synthesis.f" \
      "$src/config.json" "$src/variant.json" "$dest/input/core/"
    cmp "$GEN/rtl/cl_coralnpu.sv" "$dest/input/rtl/cl_coralnpu.sv"
    diff -qr "$PACKAGE/input/core/rtl" "$dest/input/core/rtl" > "$WORK/package_$name/rtl_diff.txt" || true
    log "$(wc -l < "$WORK/package_$name/rtl_diff.txt") RTL files differ from run 054 (rtl_diff.txt)"
    done_mark "package_$name"
    ;;

  build)
    name=${1:?build <name>}
    is_done "build_$name" && exit 0
    vivado_env
    python3 "$WORK/package_$name/rebuild/run.py" build --hdk "$HDK" --out "$WORK/build_$name"
    done_mark "build_$name"
    ;;

  audit)
    name=${1:?audit <name>}
    is_done "audit_$name" && exit 0
    vivado_env
    python3 "$WORK/package_$name/rebuild/run.py" audit \
      --checkpoint "$WORK/build_$name/output/post_route.dcp" --out "$WORK/audit_$name"
    done_mark "audit_$name"
    ;;

  tar)
    # Developer CL tarball in the same layout and manifest as run 054's
    # coralnpu.Developer_CL.tar. Submitting it (S3 upload + aws ec2
    # create-fpga-image) is a separate, approved step; see README.md.
    name=${1:?tar <name>}
    is_done "tar_$name" && exit 0
    grep -q PASS "$WORK/audit_$name/output/acceptance.txt" ||
      { echo "audit_$name has not passed; inspect it first" >&2; exit 1; }
    ts=$(date -u +%Y_%m_%d-%H%M%S)
    out=$WORK/tar_$name/to_aws
    mkdir -p "$out"
    cp "$WORK/build_$name/output/post_route.dcp" "$out/$ts.SH_CL_routed.dcp"
    cat > "$out/$ts.manifest.txt" <<EOF
pci_device_id=0xF001
pci_vendor_id=0x1D0F
pci_subsystem_id=0x1D51
pci_subsystem_vendor_id=0xFEDC
manifest_format_version=2
dcp_hash=$(sha256sum "$out/$ts.SH_CL_routed.dcp" | cut -d' ' -f1)
shell_version=0x10212415
hdk_version=2.3.0
tool_version=v2025.2
date=$ts
clock_recipe_a=A0
clock_recipe_b=B0
clock_recipe_c=C0
clock_recipe_hbm=H3
dcp_file_name=$ts.SH_CL_routed.dcp
EOF
    (cd "$WORK/tar_$name" && tar cf "$WORK/coralnpu_$name.Developer_CL.tar" to_aws)
    sha256sum "$WORK/coralnpu_$name.Developer_CL.tar"
    done_mark "tar_$name"
    ;;

  *) echo "unknown stage: $stage" >&2; exit 2 ;;
esac
