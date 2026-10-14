// Copyright 2026 Google LLC
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

// ArcSim harness for the CoralNPU AXI cores (CoreMiniAxi, RvvCoreMiniAxi,
// VmeCoreMiniAxi). It is the ArcSim counterpart of
// tests/verilator_sim/coralnpu/core_mini_axi_sim.cc and accepts the same
// command line:
//
//   *_sim --binary=<elf> [--cycles=N] [--debug_axi]
//
// Behaviour mirrors CoreMiniAxi_tb:
//   * The ELF is written into the core through its AXI slave port and read
//     back for verification; the entry point goes to CSR+0x4, then the clock
//     gate and reset are released through the CSR.
//   * The AXI master port is served by a 4 MiB memory at 0x20000000
//     (initialised to 0xa5) and a line-buffered UART at 0x54000000, like
//     tests/systemc/Xbar.h. Other addresses answer DECERR.
//   * HTIF tohost/fromhost semihosting is handled when the core exposes its
//     debug dbus ports (io_debug_dbus_*).
//   * The run ends on io_halted, io_fault or a tohost exit. Exit status is 0
//     iff there was no fault, no failing tohost exit code, and (when tohost
//     was not used) the CSR status register reads 1.
//
// The model layout comes from arcsim_model.h, generated from the state.json of
// the same build (rules/arcsim.bzl), so ports are looked up by name.

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iostream>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "absl/flags/flag.h"
#include "absl/flags/parse.h"
#include "absl/flags/usage.h"
#include "absl/log/check.h"
#include "absl/log/initialize.h"
#include "absl/log/log.h"
#include "arcsim_model.h"
#include "tests/verilator_sim/elf.h"

ABSL_FLAG(int, cycles, 100000000, "Simulation cycles");
ABSL_FLAG(bool, trace, false, "Waveform tracing (not supported by ArcSim)");
ABSL_FLAG(std::string, binary, "", "Binary to execute");
ABSL_FLAG(bool, debug_axi, false, "Enable AXI traffic debugging");
ABSL_FLAG(bool, instr_trace, false,
          "Log instructions to console (not supported by ArcSim)");
ABSL_FLAG(bool, backdoor_load, false,
          "High-speed backdoor code loading (not supported by ArcSim; the "
          "ELF is loaded over AXI)");

// Runtime entry points. Which ones exist depends on the ArcSim build; the
// @arcsim repository probes the runtime archives and defines ARCSIM_HAS_*.
#if defined(ARCSIM_HAS_STEP_ONCE)
extern "C" uint8_t* arcRuntimeIR_simStepOnce(uint8_t*, unsigned,
                                             void (*)(uint8_t*));
#endif
#if defined(ARCSIM_HAS_STEP_SETTLED)
extern "C" uint8_t* arcRuntimeIR_simStepSettled(uint8_t*, unsigned,
                                                void (*)(uint8_t*));
#endif
#if defined(ARCSIM_HAS_SIM_NOW)
extern "C" uint64_t __circt_sim_now_fs;
#endif
#if defined(ARCSIM_HAS_PLUSARGS)
extern "C" void circt_sv_set_plusargs(const char*);
#endif

namespace {

using U128 = unsigned __int128;
using arcsim_model::Port;

#ifdef HIGHMEM_RTL
constexpr uint32_t kCsrAddr = 0x200000;
#else
constexpr uint32_t kCsrAddr = 0x30000;
#endif
constexpr uint32_t kMemoryAddr = 0x20000000;
constexpr uint32_t kMemorySizeBytes = 0x400000;
constexpr uint32_t kUartAddr = 0x54000000;
constexpr uint64_t kHalfPeriodFs = 500000;  // 1 ns clock, as in Sysc_tb.
constexpr uint8_t kRespOkay = 0;
constexpr uint8_t kRespDecErr = 3;

U128 Mask(uint32_t bits) {
  return bits >= 128 ? ~U128(0) : (U128(1) << bits) - 1;
}

std::string Hex(uint64_t v) {
  char buf[32];
  std::snprintf(buf, sizeof(buf), "0x%llx", static_cast<unsigned long long>(v));
  return buf;
}

// ---------------------------------------------------------------------------
// Model: owns the state buffer and gives name-based port access.
// ---------------------------------------------------------------------------
class Model {
 public:
  Model() : state_(arcsim_model::kStateBytes, 0) {
    for (size_t i = 0; i < arcsim_model::kNumPorts; ++i) {
      const Port& p = arcsim_model::kPorts[i];
      ports_.emplace(p.name, &p);
    }
    arcsim_model::Initial(state_.data());
  }
  ~Model() { arcsim_model::Final(state_.data()); }

  const Port* Find(std::string_view name) const {
    auto it = ports_.find(std::string(name));
    return it == ports_.end() ? nullptr : it->second;
  }
  const Port* Require(std::string_view name) const {
    const Port* p = Find(name);
    CHECK(p != nullptr) << "ArcSim model " << arcsim_model::kTopName
                        << " has no port " << name;
    return p;
  }

  static U128 Load(const std::vector<uint8_t>& s, const Port* p) {
    CHECK(p->bits <= 128) << p->name << " is wider than 128 bits";
    U128 v = 0;
    std::memcpy(&v, s.data() + p->offset, (p->bits + 7) / 8);
    return v & Mask(p->bits);
  }
  U128 Get(const Port* p) const { return Load(state_, p); }
  void Set(const Port* p, U128 value) {
    CHECK(p->is_input) << p->name << " is not an input";
    CHECK(p->bits <= 128) << p->name << " is wider than 128 bits";
    value &= Mask(p->bits);
    std::memcpy(state_.data() + p->offset, &value, (p->bits + 7) / 8);
  }
  // Optional inputs (e.g. AXI prot/cache/qos) are left at their reset value
  // when a core does not have them.
  void SetIf(const Port* p, U128 value) {
    if (p) Set(p, value);
  }

  void Step() {
#if defined(ARCSIM_HAS_SIM_NOW)
    __circt_sim_now_fs = now_fs_;
#endif
    const unsigned bytes = static_cast<unsigned>(state_.size());
#if defined(ARCSIM_HAS_STEP_ONCE) && defined(ARCSIM_HAS_STEP_SETTLED)
    if (arcsim_model::kSettlesInOneEval) {
      arcRuntimeIR_simStepOnce(state_.data(), bytes, &EvalThunk);
    } else {
      arcRuntimeIR_simStepSettled(state_.data(), bytes, &EvalThunk);
    }
#elif defined(ARCSIM_HAS_STEP_SETTLED)
    arcRuntimeIR_simStepSettled(state_.data(), bytes, &EvalThunk);
#elif defined(ARCSIM_HAS_STEP_ONCE)
    arcRuntimeIR_simStepOnce(state_.data(), bytes, &EvalThunk);
#else
    (void)bytes;
    arcsim_model::Eval(state_.data());
#endif
    ++evals_;
  }

  void AdvanceTime() { now_fs_ += kHalfPeriodFs; }
  const std::vector<uint8_t>& state() const { return state_; }
  uint64_t evals() const { return evals_; }

 private:
  static void EvalThunk(uint8_t* s) { arcsim_model::Eval(s); }

  std::vector<uint8_t> state_;
  std::unordered_map<std::string, const Port*> ports_;
  uint64_t now_fs_ = 0;
  uint64_t evals_ = 0;
};

// Handles for one AXI channel group; names follow the Chisel AXI bundle.
struct AxiPorts {
  const Port *aw_valid, *aw_ready, *aw_addr, *aw_id, *aw_len, *aw_size,
      *aw_burst, *aw_prot, *aw_lock, *aw_cache, *aw_qos, *aw_region;
  const Port *w_valid, *w_ready, *w_data, *w_strb, *w_last;
  const Port *b_valid, *b_ready, *b_id, *b_resp;
  const Port *ar_valid, *ar_ready, *ar_addr, *ar_id, *ar_len, *ar_size,
      *ar_burst, *ar_prot, *ar_lock, *ar_cache, *ar_qos, *ar_region;
  const Port *r_valid, *r_ready, *r_data, *r_id, *r_resp, *r_last;

  static AxiPorts Bind(const Model& m, const std::string& prefix) {
    auto req = [&](const char* s) { return m.Require(prefix + s); };
    auto opt = [&](const char* s) { return m.Find(prefix + s); };
    AxiPorts a{};
    a.aw_valid = req("write_addr_valid");
    a.aw_ready = req("write_addr_ready");
    a.aw_addr = req("write_addr_bits_addr");
    a.aw_id = opt("write_addr_bits_id");
    a.aw_len = req("write_addr_bits_len");
    a.aw_size = req("write_addr_bits_size");
    a.aw_burst = req("write_addr_bits_burst");
    a.aw_prot = opt("write_addr_bits_prot");
    a.aw_lock = opt("write_addr_bits_lock");
    a.aw_cache = opt("write_addr_bits_cache");
    a.aw_qos = opt("write_addr_bits_qos");
    a.aw_region = opt("write_addr_bits_region");
    a.w_valid = req("write_data_valid");
    a.w_ready = req("write_data_ready");
    a.w_data = req("write_data_bits_data");
    a.w_strb = req("write_data_bits_strb");
    a.w_last = req("write_data_bits_last");
    a.b_valid = req("write_resp_valid");
    a.b_ready = req("write_resp_ready");
    a.b_id = opt("write_resp_bits_id");
    a.b_resp = req("write_resp_bits_resp");
    a.ar_valid = req("read_addr_valid");
    a.ar_ready = req("read_addr_ready");
    a.ar_addr = req("read_addr_bits_addr");
    a.ar_id = opt("read_addr_bits_id");
    a.ar_len = req("read_addr_bits_len");
    a.ar_size = req("read_addr_bits_size");
    a.ar_burst = req("read_addr_bits_burst");
    a.ar_prot = opt("read_addr_bits_prot");
    a.ar_lock = opt("read_addr_bits_lock");
    a.ar_cache = opt("read_addr_bits_cache");
    a.ar_qos = opt("read_addr_bits_qos");
    a.ar_region = opt("read_addr_bits_region");
    a.r_valid = req("read_data_valid");
    a.r_ready = req("read_data_ready");
    a.r_data = req("read_data_bits_data");
    a.r_id = opt("read_data_bits_id");
    a.r_resp = req("read_data_bits_resp");
    a.r_last = req("read_data_bits_last");
    return a;
  }
};

// ---------------------------------------------------------------------------
// Testbench
// ---------------------------------------------------------------------------
class ArcsimTb {
 public:
  ArcsimTb(uint64_t max_cycles, bool debug_axi)
      : max_cycles_(max_cycles),
        debug_axi_(debug_axi),
        memory_(kMemorySizeBytes, 0xa5) {
    clk_ = model_.Require("io_aclk");
    resetn_ = model_.Require("io_aresetn");
    halted_ = model_.Require("io_halted");
    fault_ = model_.Require("io_fault");
    slave_ = AxiPorts::Bind(model_, "io_axi_slave_");
    master_ = AxiPorts::Bind(model_, "io_axi_master_");
    CHECK(!slave_.aw_ready->is_input && master_.aw_ready->is_input)
        << "unexpected AXI port directions";
    bus_bytes_ = master_.r_data->bits / 8;
    slave_bytes_ = slave_.w_data->bits / 8;
    CHECK(bus_bytes_ <= 16 && slave_bytes_ <= 16) << "AXI data wider than 128";

    dbus_valid_ = model_.Find("io_debug_dbus_valid");
    dbus_write_ = model_.Find("io_debug_dbus_bits_write");
    dbus_addr_ = model_.Find("io_debug_dbus_bits_addr");
    dbus_wdata_ = model_.Find("io_debug_dbus_bits_wdata");
    has_dbus_ = dbus_valid_ && dbus_write_ && dbus_addr_ && dbus_wdata_;

    sampled_ = model_.state();
    model_.Step();
  }

  // Holds reset for a few cycles, then releases it (core stays clock-gated
  // and in its CSR reset until the ELF is loaded).
  void Reset() {
    model_.Set(resetn_, 0);
    for (int i = 0; i < 10; ++i) Cycle();
    model_.Set(resetn_, 1);
    model_.Step();
    for (int i = 0; i < 2; ++i) Cycle();
  }

  bool LoadElf(const std::string& path);
  void Start();
  bool Run();
  bool CheckStatus();

  uint64_t cycles() const { return cycles_; }
  uint64_t evals() const { return model_.evals(); }
  bool fault() const { return fault_seen_; }
  bool tohost_halt() const { return tohost_halt_; }
  uint32_t tohost_val() const { return tohost_val_; }
  bool timed_out() const { return timed_out_; }

 private:
  // One AXI-slave transaction issued by the testbench.
  struct SlaveTxn {
    bool write;
    uint32_t addr;
    uint8_t size;     // log2 bytes per beat
    U128 data;        // write data, lane aligned
    uint32_t strb;    // write strobes
    // Results.
    bool done = false;
    U128 rdata = 0;
    uint8_t resp = 0;
  };
  enum class SlaveState { kIdle, kAddr, kData, kResp };

  U128 Old(const Port* p) const { return Model::Load(sampled_, p); }

  void Cycle();
  void OnPosedge();
  void ServeMaster();
  void DriveSlave();
  void WatchDbus();
  void SetMasterReady();

  // Blocking AXI-slave access. Returns false on an error response.
  bool SlaveAccess(SlaveTxn& txn);
  bool Write32(uint32_t addr, uint32_t value);
  bool Read32(uint32_t addr, uint32_t* value);
  // Byte-granular target memory access, used for ELF load and semihosting.
  // Addresses inside the testbench memory are accessed directly, everything
  // else goes through the core's AXI slave (as CoreMiniAxi_tb does).
  bool WriteMem(uint64_t addr, const uint8_t* data, size_t len);
  bool ReadMem(uint64_t addr, uint8_t* data, size_t len);
  bool InHostMemory(uint64_t addr, size_t len) const {
    return addr >= kMemoryAddr && addr + len <= uint64_t(kMemoryAddr) + kMemorySizeBytes;
  }

  void ServiceTohost(uint64_t addr);
  int64_t Syscall(const uint64_t words[8]);
  void UartByte(uint8_t c);

  Model model_;
  std::vector<uint8_t> sampled_;
  const Port *clk_, *resetn_, *halted_, *fault_;
  const Port *dbus_valid_, *dbus_write_, *dbus_addr_, *dbus_wdata_;
  bool has_dbus_ = false;
  AxiPorts slave_{}, master_{};
  uint32_t bus_bytes_ = 16, slave_bytes_ = 16;

  uint64_t max_cycles_;
  bool debug_axi_;
  uint64_t cycles_ = 0;
  bool timed_out_ = false;
  bool fault_seen_ = false;

  // AXI master target (testbench memory/UART).
  std::vector<uint8_t> memory_;
  std::string uart_line_;
  bool rd_busy_ = false, wr_busy_ = false;
  uint32_t rd_addr_ = 0, wr_addr_ = 0, rd_left_ = 0, wr_left_ = 0;
  uint32_t rd_size_ = 0, wr_size_ = 0, rd_id_ = 0, wr_id_ = 0;
  uint32_t rd_burst_ = 1, wr_burst_ = 1;
  uint8_t wr_resp_ = kRespOkay;

  // AXI slave initiator.
  SlaveTxn* slave_txn_ = nullptr;
  SlaveState slave_state_ = SlaveState::kIdle;

  // HTIF.
  std::optional<uint64_t> tohost_addr_, tohost_ready_addr_, fromhost_addr_,
      fromhost_ready_addr_;
  std::deque<uint64_t> tohost_events_;
  bool tohost_halt_ = false;
  uint32_t tohost_val_ = 0;
  std::map<int64_t, int> fd_map_ = {{0, 0}, {1, 1}, {2, 2}, {-100, AT_FDCWD}};
};

// One full clock period: falling edge, then rising edge. Testbench-driven
// inputs change right after the rising edge using the pre-edge snapshot, the
// same non-blocking discipline as an RTL testbench.
void ArcsimTb::Cycle() {
  model_.Set(clk_, 0);
  model_.AdvanceTime();
  model_.Step();

  sampled_ = model_.state();
  model_.Set(clk_, 1);
  model_.AdvanceTime();
  model_.Step();
  ++cycles_;

  OnPosedge();
  model_.Step();

  if (cycles_ >= max_cycles_) timed_out_ = true;
}

void ArcsimTb::OnPosedge() {
  if (!Old(resetn_)) {
    rd_busy_ = wr_busy_ = false;
    model_.Set(master_.r_valid, 0);
    model_.Set(master_.b_valid, 0);
    SetMasterReady();
    return;
  }
  ServeMaster();
  DriveSlave();
  WatchDbus();
  if (Old(fault_)) fault_seen_ = true;
}

void ArcsimTb::SetMasterReady() {
  const bool rst = model_.Get(resetn_);
  model_.Set(master_.ar_ready,
             rst && !rd_busy_ && !model_.Get(master_.r_valid));
  model_.Set(master_.aw_ready,
             rst && !wr_busy_ && !model_.Get(master_.b_valid));
  model_.Set(master_.w_ready, rst && wr_busy_ && !model_.Get(master_.b_valid));
}

// AXI master target: one outstanding read and one outstanding write, single
// cycle latency. Supports FIXED and INCR bursts.
void ArcsimTb::ServeMaster() {
  const AxiPorts& m = master_;
  const bool rb = rd_busy_;
  const bool wb = wr_busy_;
  auto next_addr = [](uint32_t addr, uint32_t size, uint32_t burst) {
    return burst == 0 ? addr : addr + (1u << size);
  };

  if (Old(m.ar_valid) && Old(m.ar_ready)) {
    rd_busy_ = true;
    rd_addr_ = static_cast<uint32_t>(Old(m.ar_addr));
    rd_left_ = static_cast<uint32_t>(Old(m.ar_len));
    rd_size_ = static_cast<uint32_t>(Old(m.ar_size));
    rd_burst_ = static_cast<uint32_t>(Old(m.ar_burst));
    rd_id_ = m.ar_id ? static_cast<uint32_t>(Old(m.ar_id)) : 0;
    CHECK(rd_burst_ <= 1) << "unsupported AXI read burst type " << rd_burst_;
    if (debug_axi_) {
      LOG(INFO) << "AXI master AR addr=" << Hex(rd_addr_) << " len=" << rd_left_
                << " size=" << rd_size_ << " id=" << rd_id_;
    }
  }
  if (rb && !Old(m.r_valid)) {
    const uint32_t base = rd_addr_ & ~(bus_bytes_ - 1);
    U128 data = 0;
    uint8_t resp = kRespOkay;
    if (InHostMemory(base, bus_bytes_)) {
      std::memcpy(&data, &memory_[base - kMemoryAddr], bus_bytes_);
    } else {
      resp = kRespDecErr;
    }
    model_.Set(m.r_data, data);
    model_.SetIf(m.r_id, rd_id_);
    model_.Set(m.r_resp, resp);
    model_.Set(m.r_last, rd_left_ == 0);
    model_.Set(m.r_valid, 1);
  }
  if (Old(m.r_valid) && Old(m.r_ready)) {
    model_.Set(m.r_valid, 0);
    if (rd_left_ == 0) {
      rd_busy_ = false;
    } else {
      --rd_left_;
      rd_addr_ = next_addr(rd_addr_, rd_size_, rd_burst_);
    }
  }

  if (Old(m.aw_valid) && Old(m.aw_ready)) {
    wr_busy_ = true;
    wr_addr_ = static_cast<uint32_t>(Old(m.aw_addr));
    wr_left_ = static_cast<uint32_t>(Old(m.aw_len));
    wr_size_ = static_cast<uint32_t>(Old(m.aw_size));
    wr_burst_ = static_cast<uint32_t>(Old(m.aw_burst));
    wr_id_ = m.aw_id ? static_cast<uint32_t>(Old(m.aw_id)) : 0;
    wr_resp_ = kRespOkay;
    CHECK(wr_burst_ <= 1) << "unsupported AXI write burst type " << wr_burst_;
    if (debug_axi_) {
      LOG(INFO) << "AXI master AW addr=" << Hex(wr_addr_) << " len=" << wr_left_
                << " size=" << wr_size_ << " id=" << wr_id_;
    }
  }
  if (Old(m.w_valid) && Old(m.w_ready)) {
    CHECK(wb && static_cast<bool>(Old(m.w_last)) == (wr_left_ == 0))
        << "AXI WLAST mismatch";
    const uint32_t base = wr_addr_ & ~(bus_bytes_ - 1);
    const U128 data = Old(m.w_data);
    const uint32_t strb = static_cast<uint32_t>(Old(m.w_strb));
    for (uint32_t i = 0; i < bus_bytes_; ++i) {
      if (!(strb & (1u << i))) continue;
      const uint8_t byte = static_cast<uint8_t>(data >> (8 * i));
      const uint32_t addr = base + i;
      if (InHostMemory(addr, 1)) {
        memory_[addr - kMemoryAddr] = byte;
      } else if (base == kUartAddr) {
        UartByte(byte);
      } else {
        wr_resp_ = kRespDecErr;
      }
    }
    if (wr_left_ == 0) {
      wr_busy_ = false;
      model_.SetIf(m.b_id, wr_id_);
      model_.Set(m.b_resp, wr_resp_);
      model_.Set(m.b_valid, 1);
    } else {
      --wr_left_;
      wr_addr_ = next_addr(wr_addr_, wr_size_, wr_burst_);
    }
  }
  if (Old(m.b_valid) && Old(m.b_ready)) model_.Set(m.b_valid, 0);
  SetMasterReady();
}

void ArcsimTb::UartByte(uint8_t c) {
  if (c == '\n') {
    std::printf("%s\n", uart_line_.c_str());
    std::fflush(stdout);
    uart_line_.clear();
  } else {
    uart_line_.push_back(static_cast<char>(c));
  }
}

// AXI slave initiator: AW -> W -> B, or AR -> R, one transaction at a time.
void ArcsimTb::DriveSlave() {
  const AxiPorts& s = slave_;
  SlaveTxn* t = slave_txn_;
  if (!t) return;
  switch (slave_state_) {
    case SlaveState::kIdle:
      if (t->write) {
        model_.Set(s.aw_addr, t->addr);
        model_.Set(s.aw_len, 0);
        model_.Set(s.aw_size, t->size);
        model_.Set(s.aw_burst, 1);
        model_.SetIf(s.aw_id, 0);
        model_.Set(s.aw_valid, 1);
      } else {
        model_.Set(s.ar_addr, t->addr);
        model_.Set(s.ar_len, 0);
        model_.Set(s.ar_size, t->size);
        model_.Set(s.ar_burst, 1);
        model_.SetIf(s.ar_id, 0);
        model_.Set(s.ar_valid, 1);
      }
      slave_state_ = SlaveState::kAddr;
      break;
    case SlaveState::kAddr:
      if (t->write && Old(s.aw_valid) && Old(s.aw_ready)) {
        model_.Set(s.aw_valid, 0);
        model_.Set(s.w_data, t->data);
        model_.Set(s.w_strb, t->strb);
        model_.Set(s.w_last, 1);
        model_.Set(s.w_valid, 1);
        slave_state_ = SlaveState::kData;
      } else if (!t->write && Old(s.ar_valid) && Old(s.ar_ready)) {
        model_.Set(s.ar_valid, 0);
        model_.Set(s.r_ready, 1);
        slave_state_ = SlaveState::kResp;
      }
      break;
    case SlaveState::kData:
      if (Old(s.w_valid) && Old(s.w_ready)) {
        model_.Set(s.w_valid, 0);
        model_.Set(s.b_ready, 1);
        slave_state_ = SlaveState::kResp;
      }
      break;
    case SlaveState::kResp:
      if (t->write && Old(s.b_valid) && Old(s.b_ready)) {
        model_.Set(s.b_ready, 0);
        t->resp = static_cast<uint8_t>(Old(s.b_resp));
        t->done = true;
      } else if (!t->write && Old(s.r_valid) && Old(s.r_ready)) {
        model_.Set(s.r_ready, 0);
        t->rdata = Old(s.r_data);
        t->resp = static_cast<uint8_t>(Old(s.r_resp));
        t->done = true;
      }
      if (t->done) {
        if (debug_axi_) {
          LOG(INFO) << "AXI slave " << (t->write ? "W" : "R")
                    << " addr=" << Hex(t->addr) << " resp=" << int(t->resp);
        }
        slave_txn_ = nullptr;
        slave_state_ = SlaveState::kIdle;
      }
      break;
  }
}

// Mirrors CoreMiniAxi_tb::posedge(): watch core data-bus writes to the HTIF
// mailbox symbols.
void ArcsimTb::WatchDbus() {
  if (!has_dbus_ || !Old(dbus_valid_) || !Old(dbus_write_)) return;
  const uint64_t addr = static_cast<uint64_t>(Old(dbus_addr_));
  const uint32_t wdata0 = static_cast<uint32_t>(Old(dbus_wdata_));
  if (tohost_ready_addr_) {
    if (addr == *tohost_ready_addr_ && wdata0 == 1) {
      tohost_events_.push_back(0);  // Read the payload from tohost.
    }
    return;
  }
  if (tohost_addr_ && addr == *tohost_addr_ && wdata0 != 0) {
    if (wdata0 & 1) {
      tohost_halt_ = true;
      tohost_val_ = wdata0;
    } else {
      tohost_events_.push_back(wdata0);
    }
  }
}

bool ArcsimTb::SlaveAccess(SlaveTxn& txn) {
  CHECK(slave_txn_ == nullptr);
  slave_txn_ = &txn;
  while (!txn.done) {
    if (timed_out_) {
      slave_txn_ = nullptr;
      return false;
    }
    Cycle();
  }
  return txn.resp == kRespOkay;
}

bool ArcsimTb::Write32(uint32_t addr, uint32_t value) {
  CHECK_EQ(addr & 3u, 0u);
  const uint32_t lane = addr & (slave_bytes_ - 1);
  SlaveTxn t{/*write=*/true, addr, /*size=*/2, U128(value) << (8 * lane),
             0xfu << lane};
  return SlaveAccess(t);
}

bool ArcsimTb::Read32(uint32_t addr, uint32_t* value) {
  CHECK_EQ(addr & 3u, 0u);
  const uint32_t lane = addr & (slave_bytes_ - 1);
  SlaveTxn t{/*write=*/false, addr, /*size=*/2, 0, 0};
  if (!SlaveAccess(t)) return false;
  *value = static_cast<uint32_t>(t.rdata >> (8 * lane));
  return true;
}

bool ArcsimTb::WriteMem(uint64_t addr, const uint8_t* data, size_t len) {
  if (InHostMemory(addr, len)) {
    std::memcpy(&memory_[addr - kMemoryAddr], data, len);
    return true;
  }
  uint8_t size = 0;
  while ((1u << size) < slave_bytes_) ++size;
  while (len > 0) {
    const uint32_t base = static_cast<uint32_t>(addr) & ~(slave_bytes_ - 1);
    const uint32_t lane = static_cast<uint32_t>(addr) - base;
    const size_t n = std::min<size_t>(len, slave_bytes_ - lane);
    U128 word = 0;
    uint32_t strb = 0;
    for (size_t i = 0; i < n; ++i) {
      word |= U128(data[i]) << (8 * (lane + i));
      strb |= 1u << (lane + i);
    }
    SlaveTxn t{/*write=*/true, base, size, word, strb};
    if (!SlaveAccess(t)) return false;
    addr += n;
    data += n;
    len -= n;
  }
  return true;
}

bool ArcsimTb::ReadMem(uint64_t addr, uint8_t* data, size_t len) {
  if (InHostMemory(addr, len)) {
    std::memcpy(data, &memory_[addr - kMemoryAddr], len);
    return true;
  }
  uint8_t size = 0;
  while ((1u << size) < slave_bytes_) ++size;
  while (len > 0) {
    const uint32_t base = static_cast<uint32_t>(addr) & ~(slave_bytes_ - 1);
    const uint32_t lane = static_cast<uint32_t>(addr) - base;
    const size_t n = std::min<size_t>(len, slave_bytes_ - lane);
    SlaveTxn t{/*write=*/false, base, size, 0, 0};
    if (!SlaveAccess(t)) return false;
    for (size_t i = 0; i < n; ++i) {
      data[i] = static_cast<uint8_t>(t.rdata >> (8 * (lane + i)));
    }
    addr += n;
    data += n;
    len -= n;
  }
  return true;
}

bool ArcsimTb::LoadElf(const std::string& path) {
  const int fd = open(path.c_str(), O_RDONLY);
  if (fd < 0) {
    LOG(ERROR) << "Cannot open " << path;
    return false;
  }
  struct stat sb;
  CHECK_EQ(fstat(fd, &sb), 0);
  const size_t file_size = static_cast<size_t>(sb.st_size);
  void* file_data = mmap(nullptr, file_size, PROT_READ, MAP_PRIVATE, fd, 0);
  close(fd);
  CHECK(file_data != MAP_FAILED);
  const uint8_t* data8 = static_cast<const uint8_t*>(file_data);

  bool ok = true;
  uint32_t entry = 0;
  if (file_size >= 4 && std::memcmp(data8, "\177ELF", 4) == 0) {
    entry = static_cast<uint32_t>(::LoadElf(
        data8, [this, &ok](void* dest, const void* src, size_t count) {
          const uint64_t addr = reinterpret_cast<uint64_t>(dest);
          const uint8_t* bytes = static_cast<const uint8_t*>(src);
          std::vector<uint8_t> check(count);
          if (!WriteMem(addr, bytes, count) ||
              !ReadMem(addr, check.data(), count) ||
              std::memcmp(check.data(), bytes, count) != 0) {
            LOG(ERROR) << "ELF segment load/verify failed at " << Hex(addr);
            ok = false;
          }
          return dest;
        }));
    uint64_t sym;
    if (::LookupSymbol(data8, "tohost", &sym)) {
      CHECK_EQ(sym & 0xFULL, 0u);
      tohost_addr_ = sym;
    }
    if (::LookupSymbol(data8, "tohost_ready", &sym)) tohost_ready_addr_ = sym;
    if (::LookupSymbol(data8, "fromhost", &sym)) fromhost_addr_ = sym;
    if (::LookupSymbol(data8, "fromhost_ready", &sym)) fromhost_ready_addr_ = sym;
    if ((tohost_addr_ || tohost_ready_addr_) && !has_dbus_) {
      LOG(WARNING) << arcsim_model::kTopName
                   << " has no io_debug_dbus_* ports; HTIF tohost/semihosting "
                      "is unavailable (same as the Verilator testbench).";
    }
  } else {
    // Raw binary: fill ITCM from address 0.
    ok = WriteMem(0, data8, file_size);
  }
  munmap(file_data, file_size);
  if (!ok) return false;

  uint32_t readback = 0;
  return Write32(kCsrAddr + 0x4, entry) && Read32(kCsrAddr + 0x4, &readback) &&
         readback == entry;
}

// Same sequence as core_mini_axi_sim.cc: ClockGate(false), then Reset(false).
void ArcsimTb::Start() {
  uint32_t v = 0;
  CHECK(Write32(kCsrAddr, 1) && Read32(kCsrAddr, &v) && v == 1)
      << "failed to release the clock gate";
  CHECK(Write32(kCsrAddr, 0)) << "failed to release reset";
}

bool ArcsimTb::Run() {
  while (!timed_out_) {
    if (fault_seen_ || tohost_halt_ || model_.Get(halted_) ||
        model_.Get(fault_)) {
      fault_seen_ |= static_cast<bool>(model_.Get(fault_));
      return true;
    }
    if (!tohost_events_.empty()) {
      const uint64_t addr = tohost_events_.front();
      tohost_events_.pop_front();
      ServiceTohost(addr);
      continue;
    }
    Cycle();
  }
  return false;
}

bool ArcsimTb::CheckStatus() {
  uint32_t status = 0;
  if (!Read32(kCsrAddr + 0x8, &status)) return false;
  if (status != 1) {
    LOG(ERROR) << "CSR status register is " << Hex(status) << ", expected 0x1";
    return false;
  }
  return true;
}

// Mirrors CoreMiniAxi_tb::tohost_reader_thread().
void ArcsimTb::ServiceTohost(uint64_t addr) {
  if (addr == 0 && tohost_addr_) {
    uint64_t payload = 0;
    ReadMem(*tohost_addr_, reinterpret_cast<uint8_t*>(&payload), 8);
    if (payload & 1) {
      tohost_halt_ = true;
      tohost_val_ = static_cast<uint32_t>(payload);
      return;
    }
    addr = payload;
  }
  if (addr == 0) return;

  uint64_t words[8];
  int64_t resp_val = -1;
  if (!ReadMem(addr, reinterpret_cast<uint8_t*>(words), sizeof(words))) {
    LOG(ERROR) << "tohost read error at " << Hex(addr);
  } else {
    if (tohost_addr_) {
      const uint64_t zero = 0;
      WriteMem(*tohost_addr_, reinterpret_cast<const uint8_t*>(&zero), 8);
    }
    resp_val = Syscall(words);
  }

  const uint32_t resp[2] = {static_cast<uint32_t>(resp_val),
                            static_cast<uint32_t>(resp_val >> 32)};
  WriteMem(addr, reinterpret_cast<const uint8_t*>(&resp[0]), 4);
  WriteMem(addr + 4, reinterpret_cast<const uint8_t*>(&resp[1]), 4);
  if (fromhost_addr_) {
    const uint32_t one[2] = {1, 0};
    WriteMem(*fromhost_addr_, reinterpret_cast<const uint8_t*>(&one[0]), 4);
    WriteMem(*fromhost_addr_ + 4, reinterpret_cast<const uint8_t*>(&one[1]), 4);
  }
  if (fromhost_ready_addr_) {
    const uint64_t one = 1;
    WriteMem(*fromhost_ready_addr_, reinterpret_cast<const uint8_t*>(&one), 8);
  }
  if (tohost_ready_addr_) {
    const uint64_t zero = 0;
    WriteMem(*tohost_ready_addr_, reinterpret_cast<const uint8_t*>(&zero), 8);
  }
}

int64_t ArcsimTb::Syscall(const uint64_t words[8]) {
  auto host_fd = [this](uint64_t guest_fd) {
    auto it = fd_map_.find(static_cast<int64_t>(guest_fd));
    return it == fd_map_.end() ? -1 : it->second;
  };
  switch (words[0]) {
    case 56: {  // openat
      const int64_t dirfd_guest = static_cast<int64_t>(words[1]);
      int dirfd = dirfd_guest == -100 ? AT_FDCWD : static_cast<int>(dirfd_guest);
      auto it = fd_map_.find(dirfd_guest);
      if (it != fd_map_.end()) dirfd = it->second;
      std::vector<char> name(static_cast<size_t>(words[3]) + 1, 0);
      if (!ReadMem(words[2], reinterpret_cast<uint8_t*>(name.data()), words[3])) {
        return -1;
      }
      const int flags = static_cast<int>(words[4]);
      int host_flags = 0;
      if (flags & 0x0001) host_flags |= O_WRONLY;
      if (flags & 0x0002) host_flags |= O_RDWR;
      if (flags & 0x0200) host_flags |= O_CREAT;
      if (flags & 0x0400) host_flags |= O_TRUNC;
      if (flags & 0x0800) host_flags |= O_EXCL;
      if (flags & 0x4000) host_flags |= O_APPEND;
      const int fd = openat(dirfd, name.data(), host_flags,
                            static_cast<int>(words[5]));
      if (fd < 0) return -1;
      const int64_t guest = fd + 3;  // Avoid overlapping guest stdio.
      fd_map_[guest] = fd;
      return guest;
    }
    case 57: {  // close
      auto it = fd_map_.find(static_cast<int64_t>(words[1]));
      if (it == fd_map_.end()) return -1;
      const int r = close(it->second);
      fd_map_.erase(it);
      return r;
    }
    case 62:  // lseek
      return lseek(host_fd(words[1]), static_cast<off_t>(words[2]),
                   static_cast<int>(words[3]));
    case 63:    // read
    case 67: {  // pread
      std::vector<uint8_t> buf(static_cast<size_t>(words[3]));
      const int fd = words[0] == 63 && words[1] == 0 ? 0 : host_fd(words[1]);
      const ssize_t n =
          words[0] == 63
              ? read(fd, buf.data(), buf.size())
              : pread(fd, buf.data(), buf.size(), static_cast<off_t>(words[4]));
      if (n > 0 && !WriteMem(words[2], buf.data(), static_cast<size_t>(n))) {
        return -1;
      }
      return n;
    }
    case 64:    // write
    case 68: {  // pwrite
      std::vector<uint8_t> buf(static_cast<size_t>(words[3]));
      if (!ReadMem(words[2], buf.data(), buf.size())) return -1;
      if (words[0] == 64 && (words[1] == 1 || words[1] == 2)) {
        std::cout.write(reinterpret_cast<const char*>(buf.data()), buf.size());
        std::cout.flush();
        return static_cast<int64_t>(buf.size());
      }
      const int fd = host_fd(words[1]);
      return words[0] == 64
                 ? write(fd, buf.data(), buf.size())
                 : pwrite(fd, buf.data(), buf.size(),
                          static_cast<off_t>(words[4]));
    }
    case 93:  // exit
      tohost_halt_ = true;
      tohost_val_ = static_cast<uint32_t>(words[1]);
      return 0;
    case 2011: {  // getmainvars
      const uint64_t buf_addr = words[1];
      const uint64_t limit = words[2];
      const char* arg0 = "coralnpu.elf";
      const uint64_t arg0_addr = buf_addr + 16;
      if (limit < 16 + std::strlen(arg0) + 1) return -1;
      const uint64_t packet[2] = {1, arg0_addr};
      WriteMem(buf_addr, reinterpret_cast<const uint8_t*>(packet), 16);
      WriteMem(arg0_addr, reinterpret_cast<const uint8_t*>(arg0),
               std::strlen(arg0) + 1);
      return 0;
    }
    default:
      return -1;
  }
}

}  // namespace

int main(int argc, char** argv) {
  absl::InitializeLog();
  absl::SetProgramUsageMessage("CoralNPU ArcSim simulator");
  std::vector<char*> args = absl::ParseCommandLine(argc, argv);

#if defined(ARCSIM_HAS_PLUSARGS)
  std::string plusargs;
  for (size_t i = 1; i < args.size(); ++i) {
    if (args[i][0] == '+') {
      if (!plusargs.empty()) plusargs += ' ';
      plusargs += args[i];
    }
  }
  circt_sv_set_plusargs(plusargs.c_str());
#endif

  const std::string binary = absl::GetFlag(FLAGS_binary);
  if (binary.empty()) {
    LOG(ERROR) << "--binary is required";
    return 1;
  }
  if (absl::GetFlag(FLAGS_trace)) {
    LOG(WARNING) << "--trace is not supported by ArcSim; no waveform written.";
  }
  if (absl::GetFlag(FLAGS_instr_trace)) {
    LOG(WARNING) << "--instr_trace is not supported by ArcSim.";
  }
  if (absl::GetFlag(FLAGS_backdoor_load)) {
    LOG(WARNING) << "--backdoor_load is not supported by ArcSim; loading the "
                    "ELF over AXI.";
  }

  const auto began = std::chrono::steady_clock::now();
  ArcsimTb tb(static_cast<uint64_t>(absl::GetFlag(FLAGS_cycles)),
              absl::GetFlag(FLAGS_debug_axi));
  tb.Reset();
  if (!tb.LoadElf(binary)) {
    LOG(ERROR) << "Failed to load " << binary;
    return 1;
  }
  const uint64_t load_cycles = tb.cycles();
  tb.Start();
  const uint64_t start = tb.cycles();
  const bool finished = tb.Run();
  const uint64_t exec_cycles = tb.cycles() - start;

  bool ok = finished && !tb.fault() &&
            !(tb.tohost_halt() && tb.tohost_val() != 1);
  if (finished && !tb.fault() && !tb.tohost_halt()) {
    ok = tb.CheckStatus() && ok;
  }

  const double wall = std::chrono::duration<double>(
                          std::chrono::steady_clock::now() - began)
                          .count();
  if (!finished) LOG(ERROR) << "Timeout after " << tb.cycles() << " cycles";
  if (tb.fault()) LOG(ERROR) << "Core reported io_fault";
  if (tb.tohost_halt() && tb.tohost_val() != 1) {
    LOG(ERROR) << "tohost exit code " << (tb.tohost_val() >> 1);
  }
  std::printf("EXECUTION_CYCLES %llu\n",
              static_cast<unsigned long long>(exec_cycles));
  std::printf(
      "ARCSIM %s model=%s cycles=%llu load_cycles=%llu evals=%llu "
      "wall_s=%.3f cycles_per_s=%.0f\n",
      ok ? "PASS" : "FAIL", arcsim_model::kTopName,
      static_cast<unsigned long long>(tb.cycles()),
      static_cast<unsigned long long>(load_cycles),
      static_cast<unsigned long long>(tb.evals()), wall,
      wall > 0 ? tb.cycles() / wall : 0.0);
  return ok ? 0 : 1;
}
