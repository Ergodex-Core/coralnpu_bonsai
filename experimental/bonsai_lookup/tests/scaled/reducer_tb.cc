#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <stdexcept>

#include "Vbonsai_group_reduce.h"
#include "verilated.h"

static uint32_t bits(float f) {
  uint32_t u;
  std::memcpy(&u, &f, 4);
  return u;
}
static float value(uint32_t u) {
  float f;
  std::memcpy(&f, &u, 4);
  return f;
}
static float half(uint16_t h) {
  float mag = (h & 0x7c00) ? std::ldexp(float(1024 + (h & 1023)), int((h >> 10) & 31) - 25)
                           : std::ldexp(float(h & 1023), -24);
  return (h & 0x8000) ? -mag : mag;
}
static uint32_t oracle(int32_t dot, uint16_t h, uint32_t a, uint32_t old) {
  volatile float p = float(dot) * half(h);
  volatile float q = p * value(a);
  volatile float y = value(old) + q;
  return bits(y);
}
int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  Vbonsai_group_reduce d;
  uint64_t cycles = 0, checks = 0;
  auto tick = [&]() {
    d.clk = 0;
    d.eval();
    d.clk = 1;
    d.eval();
    ++cycles;
  };
  d.rst_n       = 0;
  d.start_valid = 0;
  d.in_valid    = 0;
  d.out_ready   = 0;
  for (int i = 0; i < 16; ++i)
    tick();
  d.rst_n    = 1;
  auto start = [&](int groups) {
    if (!d.start_ready)
      throw std::runtime_error("start not ready");
    d.start_m      = 1;
    d.start_n      = 1;
    d.start_groups = groups;
    d.start_tag    = 0x103;
    d.start_valid  = 1;
    tick();
    d.start_valid = 0;
  };
  auto item = [&](int32_t dot, uint16_t h, uint32_t a, int group, uint32_t expected,
                  bool bad = false, bool force_fault = false, int expected_flags = -1) {
    d.out_ready           = 0;
    d.in_dot              = uint32_t(dot);
    d.in_weight_scale     = h;
    d.in_activation_scale = a;
    d.in_token            = 0;
    d.in_row              = 0;
    d.in_group            = group;
    d.in_error            = force_fault;
    d.in_valid            = 1;
    if (!d.in_ready)
      throw std::runtime_error("input not ready");
    tick();
    d.in_valid = 0;
    int wait   = 0;
    while (!d.out_valid && wait++ < 100)
      tick();
    if (!d.out_valid)
      throw std::runtime_error("timeout");
    uint32_t got = d.out_value;
    if (expected_flags >= 0 && d.out_fflags != expected_flags)
      throw std::runtime_error("fflags mismatch");
    if (got != expected || bool(d.out_error) != bad || d.out_tag != 0x103) {
      std::fprintf(stderr, "mismatch dot=%d h=%04x a=%08x g=%d got=%08x want=%08x err=%d\n", dot, h,
                   a, group, got, expected, int(d.out_error));
      throw std::runtime_error("numeric mismatch");
    }
    for (int i = 0; i < 3; ++i) {
      tick();
      if (!d.out_valid || d.out_value != got)
        throw std::runtime_error("unstable output");
    }
    d.out_ready = 1;
    tick();
    ++checks;
  };
  // Exhaustive bounded integer conversion and all finite half scale patterns.
  for (int dot = -16384; dot <= 16384; ++dot) {
    start(1);
    item(dot, 0x3c00, 0x3f800000, 0, oracle(dot, 0x3c00, 0x3f800000, 0));
  }
  for (unsigned h = 0; h < 65536; ++h)
    if ((h & 0x7c00) != 0x7c00) {
      start(1);
      item(1, h, 0x3f800000, 0, oracle(1, h, 0x3f800000, 0));
    }
  // General finite positive activation scales, including underflow/overflow,
  // cancellation and rounding of sequential operations (never fused).
  std::mt19937 rng(103);
  for (int frame = 0; frame < 1000; ++frame) {
    start(48);
    uint32_t sum = 0;
    for (int g = 0; g < 48; ++g) {
      int dot    = int(rng() % 32769) - 16384;
      uint16_t h = uint16_t(rng() % 0x7c00);
      if (rng() & 1)
        h |= 0x8000;
      uint32_t a = (rng() % 0x7f7fffff) + 1;
      sum        = oracle(dot, h, a, sum);
      // NaNs are canonicalized by fpnew; finite/infinity bits remain exact.
      if (std::isnan(value(sum)))
        sum = 0x7fc00000;
      item(dot, h, a, g, sum);
    }
  }
  for (int kind = 0; kind < 7; ++kind) {
    start(1);
    int dot    = kind == 0 ? 16385 : 1;
    uint16_t h = kind == 1 ? 0x7c00 : 0x3c00;
    uint32_t a = kind == 2 ? 0 : kind == 3 ? 0xbf800000 : kind == 4 ? 0x7f800000 : 0x3f800000;
    item(dot, h, a, kind == 5 ? 1 : 0, 0x7fc00000, true, kind == 6);
    if (!d.job_error)
      throw std::runtime_error("missing fault");
  }
  start(2);
  item(16384, 0x7bff, 0x7f7fffff, 0, 0x7f800000, false, false, 5);
  item(0, 0x3c00, 0x3f800000, 1, 0x7f800000, false, false, 5);
  start(1);
  item(1, 1, 1, 0, 0, false, false, 3);
  start(2);
  item(1, 0x3c00, 0x3f800000, 0, 0x3f800000);
  item(1, 0x3c00, 0x33800000, 1, 0x3f800000, false, false, 1);
  for (int g : {0, 49}) {
    start(g);
    if (!d.done || !d.job_error || !d.start_ready)
      throw std::runtime_error("bad shape accepted");
  }
  // Reset at every phase of an in-flight operation and restart cleanly.
  for (int phase = 0; phase < 22; ++phase) {
    start(1);
    d.in_valid            = 1;
    d.in_dot              = 17;
    d.in_weight_scale     = 0x3c00;
    d.in_activation_scale = 0x3f800000;
    d.in_group            = 0;
    d.in_error            = 0;
    d.out_ready           = 0;
    tick();
    d.in_valid = 0;
    for (int i = 0; i < phase; ++i)
      tick();
    d.rst_n = 0;
    tick();
    d.rst_n = 1;
    start(1);
    item(7, 0x3c00, 0x3f800000, 0, bits(7.0f));
  }
  std::printf("{\"status\":\"PASS\",\"checked_updates\":%llu,\"target_cycles\":%llu}\n",
              (unsigned long long)checks, (unsigned long long)cycles);
}
