// Copyright 2024 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>

#include <chrono>
#include <cstdio>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "absl/flags/flag.h"
#include "absl/flags/parse.h"
#include "absl/flags/usage.h"
#include "absl/log/check.h"
#include "absl/log/initialize.h"
#include "absl/log/log.h"
#include "tests/verilator_sim/coralnpu/core_mini_axi_tb.h"
#include "tests/verilator_sim/symbol_args.h"
#include "tests/verilator_sim/sysc_tb.h"

/* clang-format off */
#include <systemc.h>
/* clang-format on */

ABSL_FLAG(int, cycles, 100000000, "Simulation cycles");
ABSL_FLAG(bool, trace, false, "Dump VCD trace");
ABSL_FLAG(std::string, binary, "", "Binary to execute");
ABSL_FLAG(bool, debug_axi, false, "Enable AXI traffic debugging");
ABSL_FLAG(bool, instr_trace, false, "Log instructions to console");
ABSL_FLAG(bool, backdoor_load, false, "Enable high-speed backdoor code loading");
ABSL_FLAG(std::vector<std::string>, set, {},
          "Comma-separated symbol=value 32-bit writes applied after the ELF "
          "is loaded, e.g. active_m=1,active_epsilon=f32:1e-6");
ABSL_FLAG(int, axi_read_latency, 0,
          "External memory latency in cycles from AR acceptance to the first R "
          "beat. Reads are pipelined (many outstanding, like a DRAM controller).");
ABSL_FLAG(int, axi_write_latency, 0,
          "Cycles from the last W beat to the B response.");
ABSL_FLAG(std::vector<std::string>, dump, {},
          "Comma-separated symbols whose 32-bit values are printed after the "
          "run as 'SYMBOL name=value'");

static bool run(const char* name, const std::string binary, const int cycles,
                const bool trace, const bool debug_axi, const bool instr_trace,
                const bool backdoor_load,
                std::vector<std::pair<std::string, uint32_t>> overrides,
                const std::vector<std::string>& dumps) {
  const auto began = std::chrono::steady_clock::now();
  absl::Mutex halted_mtx;
  absl::CondVar halted_cv;
  CoreMiniAxi_tb tb(CoreMiniAxi_tb::kCoreMiniAxiModelName, cycles, /* random= */ false, debug_axi,
                    instr_trace, backdoor_load,
                    /*wfi_cb=*/std::nullopt,
                    /*halted_cb=*/[&halted_mtx, &halted_cv]() {
                      absl::MutexLock lock_(&halted_mtx);
                      halted_cv.SignalAll();
                    });
  // Ensure we call sc_start @0 time, before we startup anything else.
  // This allows Verilog `initial` blocks to run before we try to interact with the
  // DUT model.
  sc_start(SC_ZERO_TIME);

  if (trace) {
    tb.trace(tb.core());
  }

  tb.SetMemoryLatency(absl::GetFlag(FLAGS_axi_read_latency),
                      absl::GetFlag(FLAGS_axi_write_latency));
  tb.SetSymbolOverrides(std::move(overrides));
  std::thread sc_main_thread([&tb]() { tb.start(); });

  CHECK_OK(tb.LoadElfSync(binary));
  CHECK_OK(tb.ClockGateSync(false));
  CHECK_OK(tb.ResetAsync(false));

  {
    absl::MutexLock lock_(&halted_mtx);
    halted_cv.Wait(&halted_mtx);
  }

  if (!tb.io_fault && !tb.tohost_halt) {
    CHECK_OK(tb.CheckStatusSync());
  }

  // Read before sc_stop(): the SRAM backdoor registry is torn down when the
  // model's final blocks run.
  for (const auto& name : dumps) {
    uint32_t value = 0;
    const absl::Status status = tb.ReadSymbol(name, &value);
    if (status.ok()) {
      printf("SYMBOL %s=%u\n", name.c_str(), value);
    } else {
      LOG(ERROR) << status;
    }
  }

  sc_stop();
  sc_main_thread.join();
  const bool ok = !tb.io_fault && !(tb.tohost_halt && tb.tohost_val != 1);

  // Same format as the ArcSim harness (tests/arcsim_sim) so simulator
  // performance can be compared run for run. The testbench clock is 1 ns.
  const double wall = std::chrono::duration<double>(
                          std::chrono::steady_clock::now() - began)
                          .count();
  const unsigned long long sim_cycles = static_cast<unsigned long long>(
      sc_time_stamp() / sc_time(1, SC_NS));
  printf("VERILATOR %s model=%s cycles=%llu wall_s=%.3f cycles_per_s=%.0f\n",
         ok ? "PASS" : "FAIL", name, sim_cycles, wall,
         wall > 0 ? sim_cycles / wall : 0.0);
  fflush(stdout);
  return ok;
}

extern "C" int sc_main(int argc, char** argv) {
  absl::InitializeLog();
  absl::SetProgramUsageMessage("CoreMiniAxi simulator");
  auto args = absl::ParseCommandLine(argc, argv);

  // Pass clean arguments to Verilator.
  int v_argc = args.size();
  char** v_argv = &args[0];
  Verilated::commandArgs(v_argc, v_argv);

  if (absl::GetFlag(FLAGS_binary) == "") {
    LOG(ERROR) << "--binary is required!";
    return -1;
  }

  std::vector<std::pair<std::string, uint32_t>> overrides;
  for (const auto& arg : absl::GetFlag(FLAGS_set)) {
    auto parsed = coralnpu_sim::ParseSymbolAssignment(arg);
    if (!parsed) {
      LOG(ERROR) << "bad --set entry (want symbol=value): " << arg;
      return -1;
    }
    overrides.push_back(*parsed);
  }

  return run(Sysc_tb::get_name(argv[0]), absl::GetFlag(FLAGS_binary),
      absl::GetFlag(FLAGS_cycles), absl::GetFlag(FLAGS_trace),
      absl::GetFlag(FLAGS_debug_axi), absl::GetFlag(FLAGS_instr_trace),
      absl::GetFlag(FLAGS_backdoor_load), std::move(overrides),
      absl::GetFlag(FLAGS_dump)) ? 0 : 1;
}
