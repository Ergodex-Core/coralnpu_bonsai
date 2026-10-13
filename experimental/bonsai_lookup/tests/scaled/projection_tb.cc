#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "Vbonsai_scaled_tile.h"
#include "verilated.h"

static std::vector<uint8_t> read(const std::string &p) {
  std::ifstream f(p, std::ios::binary);
  if (!f)
    throw std::runtime_error(p);
  return {std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>()};
}
static uint32_t word(const std::vector<uint8_t> &v, size_t i) {
  if (i + 4 > v.size())
    throw std::runtime_error("word range");
  return uint32_t(v[i]) | uint32_t(v[i + 1]) << 8 | uint32_t(v[i + 2]) << 16 |
         uint32_t(v[i + 3]) << 24;
}
struct Request {
  uint32_t addr;
  int id;
  uint64_t due;
};
int main(int argc, char **argv) {
  if (argc != 8 && argc != 9)
    throw std::runtime_error("fixtures role mode N K M tile-limit");
  int fault           = argc == 9 ? std::stoi(argv[8]) : 0;
  bool expect_error   = (fault >= 1 && fault <= 5) || fault == 8 || fault == 9;
  bool invalid_job    = fault == 5 || fault == 9;
  unsigned sb_current = 0;
  bool injected       = false;
  Verilated::commandArgs(argc, argv);
  Vbonsai_scaled_tile d;
  std::string root = std::string(argv[1]) + "/" + argv[2] + "/", mode = argv[3];
  int N = std::stoi(argv[4]), K = std::stoi(argv[5]), M = std::stoi(argv[6]),
      limit = std::stoi(argv[7]), G = K / 128;
  auto w = read(root + "weights.pq2"), a = read(root + mode + ".activations.i8"),
       s          = read(root + mode + ".activation_scales.f32"),
       y          = read(root + mode + ".scaled_outputs.f32");
  uint64_t cycles = 0, total_cycles = 0, total_bytes = 0, outputs = 0, tiles = 0;
  std::vector<uint8_t> memory;
  std::vector<Request> pending;
  int held     = -1;
  bool stalled = false;
  std::array<uint32_t, 5> stable{};
  int tm = 0, rbase = 0, tbase = 0;
  unsigned count = 0;
  auto tick      = [&](bool service) {
    d.clk       = 0;
    d.rd_ready  = service && cycles % 16 < 12;
    d.out_ready = cycles % 16 < 12;
    if (service && held < 0) {
      for (size_t i = 0; i < pending.size(); ++i)
        if (pending[i].due <= cycles && (held < 0 || pending[i].id > pending[held].id))
          held = int(i);
    }
    d.rsp_valid = service && held >= 0;
    d.rsp_error = 0;
    if (d.rsp_valid) {
      auto &r  = pending[held];
      d.rsp_id = r.id;
      if ((fault == 1 && r.addr == sb_current) || (fault == 3 && r.addr == 0))
        d.rsp_error = 1;
      if (fault == 2 && r.addr == sb_current)
        d.rsp_id = 15;
      for (int j = 0; j < 4; ++j)
        d.rsp_data[j] = word(memory, r.addr + 4 * j);
    }
    d.eval();
    bool req = d.rd_valid && d.rd_ready, rsp = d.rsp_valid && d.rsp_ready;
    auto addr = d.rd_addr;
    auto id   = d.rd_id;
    if (stalled &&
        (!d.out_valid || stable != std::array<uint32_t, 5>{d.out_value, d.out_token, d.out_row,
                                                           d.out_tag, d.out_error}))
      throw std::runtime_error("unstable result");
    stalled = d.out_valid && !d.out_ready;
    stable  = {d.out_value, d.out_token, d.out_row, d.out_tag, d.out_error};
    if (d.out_valid && d.out_ready) {
      int token = int(count) / d.job_n, row = int(count) % d.job_n;
      uint32_t expected = word(y, 4 * ((tbase + token) * N + rbase + row));
      if (d.out_token != token || d.out_row != row ||
          (expect_error ? !d.out_error : (d.out_value != expected || d.out_error)) ||
          d.out_tag != 0x103) {
        std::fprintf(stderr, "tile %llu output %u token %u row %u got %08x want %08x error %u\n",
                          (unsigned long long)tiles, count, d.out_token, d.out_row, d.out_value,
                          expected, d.out_error);
        throw std::runtime_error("projection mismatch");
      }
      ++count;
      ++outputs;
    }
    d.clk = 1;
    d.eval();
    ++cycles;
    if (rsp) {
      pending.erase(pending.begin() + held);
      held = -1;
    }
    if (req) {
      if (addr + 16 > memory.size())
        throw std::runtime_error("read range");
      for (auto &r : pending)
        if (r.id == id)
          throw std::runtime_error("reused live ID");
      pending.push_back({addr, int(id), cycles + 7});
    }
  };
  d.rst_n     = 0;
  d.job_valid = 0;
  for (int i = 0; i < 16; ++i)
    tick(false);
  d.rst_n = 1;
  for (tbase = 0; tbase < M; tbase += 8)
    for (rbase = 0; rbase < N; rbase += 32) {
      if (limit && int(tiles) >= limit)
        goto finish;
      tm     = std::min(8, M - tbase);
      int tn = std::min(32, N - rbase);
      if (fault == 7)
        --tn;
      unsigned ab = G * 1088, sb = ab + G * tm * 128;
      sb_current = sb;
      memory.assign(sb + G * 32, 0);
      for (int g = 0; g < G; ++g) {
        for (int r = 0; r < 32; ++r) {
          size_t dest = g * 1088 + r * 32;
          if (r < tn) {
            size_t src = ((rbase + r) * G + g) * 34;
            std::memcpy(&memory[dest], &w[src + 2], 32);
            memory[g * 1088 + 1024 + r * 2] = w[src];
            memory[g * 1088 + 1025 + r * 2] = w[src + 1];
          } else
            std::memset(&memory[dest], 0x55, 32);
        }
        for (int t = 0; t < tm; ++t) {
          std::memcpy(&memory[ab + (g * tm + t) * 128], &a[(tbase + t) * K + g * 128], 128);
          std::memcpy(&memory[sb + g * 32 + t * 4], &s[((tbase + t) * G + g) * 4], 4);
        }
      }
      if (fault == 4)
        memory[0] |= 3;
      if (fault == 8)
        std::memset(&memory[sb], 0, 4);
      if (!d.job_ready || !pending.empty())
        throw std::runtime_error("unclean start");
      d.job_m           = tm;
      d.job_n           = tn;
      d.job_groups      = G;
      d.job_tag         = 0x103;
      d.weight_base     = 0;
      d.activation_base = ab;
      d.scale_base      = sb;
      if (fault == 5)
        d.job_m = 0;
      if (fault == 9)
        d.weight_base = 0xfffffc00;
      d.job_valid = 1;
      count       = 0;
      tick(true);
      d.job_valid    = 0;
      uint64_t begin = cycles;
      while (!d.done && cycles - begin < 1000000) {
        tick(true);
        if (fault == 6 && !injected && cycles - begin == 20) {
          d.rst_n = 0;
          pending.clear();
          held    = -1;
          stalled = false;
          for (int i = 0; i < 16; ++i)
            tick(false);
          d.rst_n     = 1;
          d.job_valid = 1;
          tick(true);
          d.job_valid = 0;
          begin       = cycles;
          injected    = true;
        }
      }
      if (!d.done || bool(d.job_error) != expect_error ||
          count != (invalid_job ? 0 : unsigned(tm * tn)) || !pending.empty())
        throw std::runtime_error("job completion failure");
      if (d.bytes_read != (invalid_job ? 0 : unsigned(G * (1088 + tm * 128 + 32))))
        throw std::runtime_error("byte accounting");
      if (d.cycles_total != cycles - begin)
        throw std::runtime_error("cycle accounting");
      total_cycles += d.cycles_total;
      total_bytes += d.bytes_read;
      ++tiles;
    }
finish:
  std::printf(
      "{\"status\":\"PASS\",\"tiles\":%llu,\"outputs\":%llu,\"target_cycles\":%llu,\"accepted_read_"
      "bytes\":%llu,\"command_issue_cycles\":%llu}\n",
      (unsigned long long)tiles, (unsigned long long)outputs, (unsigned long long)total_cycles,
      (unsigned long long)total_bytes, (unsigned long long)tiles);
}
