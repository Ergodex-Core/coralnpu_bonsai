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
//
// Kernel launch ABI for the CoralNPU F2 image (8 KiB ITCM, 32 KiB DTCM).
//
// Every kernel is its own ELF. The host writes a `KernelArgs` block at the
// `kargs` symbol (DTCM), sets the start PC and releases reset; the kernel
// reads its operands from DDR (0x80000000) / HBM (0xC0000000) through the
// core's data bus, writes results back there, stores its status in
// `kargs.status`, and halts with `main` returning 0 (`_ret == 0`).
//
// All pointers are 32-bit core addresses. Activation buffers always have
// MAX_TOKENS (16) rows so vector loads with vl=16 never leave the buffer.
#ifndef MODELS_COMMON_KERNEL_ARGS_H_
#define MODELS_COMMON_KERNEL_ARGS_H_

#include <stdint.h>

#define MAX_TOKENS 16     // rows per activation buffer (= matrix tile edge)
#define Q4_GROUP 32       // Q4_0 block: 32 weights per f32 scale
#define TILE_N 16         // output channels per matrix tile
#define STATUS_OK 0x600D
#define STATUS_BAD_ARGS 0xBAD0

typedef struct {
  // Common.
  uint32_t m;          // active token rows (1..16)
  uint32_t k;          // reduction length (multiple of 32)
  uint32_t n;          // output channels (multiple of 16)
  uint32_t flags;      // kernel specific
  // Operands (core addresses).
  uint32_t in0, in1, in2, in3;
  uint32_t out0, out1;
  // Scalars.
  float f0, f1;
  uint32_t u0, u1, u2, u3;
  // Written by the kernel.
  volatile uint32_t status;
  volatile uint32_t cycles;
} KernelArgs;

// Weight tile layout (see host/weights.py pack_tiles):
//   for tile t in n/16, for group g in k/32, for k4 in 0..7:
//     32 bytes: byte[lane]      = nib(k0+0) | nib(k0+1) << 4   lane = channel
//               byte[16 + lane] = nib(k0+2) | nib(k0+3) << 4   k0 = g*32 + k4*4
//   nib = q + 8 in 0..15 (Q4_0 code), weight = (nib - 8) * scale[t][g][lane].
#define TILE_GROUP_BYTES 256
#define TILE_BYTES(k) ((k) / Q4_GROUP * TILE_GROUP_BYTES)

static inline uint32_t rdcycle(void) {
  uint32_t c;
  __asm__ volatile("csrr %0, mcycle" : "=r"(c));
  return c;
}

// Each kernel defines `kargs` in .data (the host locates it by symbol) and a
// `run(KernelArgs*)` that returns 0 on success.
#define DEFINE_KERNEL(run_fn)                                                 \
  KernelArgs kargs __attribute__((section(".data"), aligned(16)));           \
  int main(void) {                                                           \
    uint32_t t0 = rdcycle();                                                 \
    int rc = run_fn(&kargs);                                                 \
    kargs.cycles = rdcycle() - t0;                                           \
    kargs.status = rc ? STATUS_BAD_ARGS : STATUS_OK;                         \
    return rc;                                                               \
  }

#endif  // MODELS_COMMON_KERNEL_ARGS_H_
