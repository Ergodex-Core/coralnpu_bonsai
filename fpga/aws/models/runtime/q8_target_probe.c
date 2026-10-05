/* SPDX-License-Identifier: Apache-2.0
 * Small on-device Q8 integer/quantization/matvec probe. No model weights,
 * host operators or external libraries. Link with the owner's qualified CRT.
 * Exported q8_probe_result must only be read after halt/drain.
 */
#include <stdint.h>
#include <stddef.h>
#include "q8_0.h"
volatile uint32_t q8_probe_seed = 19;
volatile uint32_t q8_probe_result[8] = {0x51385031u, 0, 0, 0, 0, 0, 0, 0};
/* Satisfies existing model linker mailbox reservation, unused by this probe. */
volatile unsigned char coral_mailbox[128] __attribute__((section(".mailbox"), aligned(64)));
/* The linked HBM probe exercises vector reads from the external window. */
static unsigned char weights[33 * 2 * 34] __attribute__((section(".q8_probe_data"), aligned(64)));
static unsigned char a[32], b[32], quant[34];
static float input[64], output[33];
void __cxa_finalize(void) {}
#ifdef __riscv
__attribute__((naked)) void coralnpu_exception_handler(void) {
  __asm__ volatile("ebreak\n1: j 1b");
}
#endif
void *memset(void *dst, int value, size_t n) {
  unsigned char *p = dst;
  for (size_t i = 0; i < n; ++i) p[i] = (unsigned char)value;
  return dst;
}
void *memcpy(void *dst, const void *src, size_t n) {
  unsigned char *d = dst; const unsigned char *s = src;
  for (size_t i = 0; i < n; ++i) d[i] = s[i];
  return dst;
}
static int fail(unsigned stage, int observed, int expected) {
  q8_probe_result[1] = stage;
  q8_probe_result[2] = (uint32_t)observed;
  q8_probe_result[3] = (uint32_t)expected;
  return 1;
}
int main(void) {
#ifdef __riscv
  __asm__ volatile("csrwi fcsr, 0" ::: "memory");
#endif
  q8_probe_result[1] = 1;
  for (unsigned i = 0; i < 32; ++i) a[i] = b[i] = 128;
  int dot = cm_q8_dot32(a, b);
  if (dot != 524288) return fail(2, dot, 524288);
  static const float pattern[8] = {127,-127,.5f,-.5f,1.5f,-1.5f,126.5f,-126.5f};
  static const int codes[8] = {127,-127,1,-1,2,-2,127,-127};
  for (unsigned i = 0; i < 64; ++i) input[i] = pattern[i % 8];
  if (!cm_q8_quant32(quant, input)) return fail(3, 0, 1);
  if (quant[0] || quant[1] != 0x3c) return fail(4, quant[0] + 256*quant[1], 0x3c00);
  for (unsigned i = 0; i < 32; ++i)
    if (quant[i+2] != (unsigned char)(codes[i%8] & 255)) return fail(5, quant[i+2], codes[i%8]);
  unsigned seed = q8_probe_seed & 255u;
  for (unsigned r = 0; r < 33; ++r) {
    for (unsigned block = 0; block < 2; ++block) {
      unsigned char *w = weights + (r*2+block)*34;
      w[0] = 0; w[1] = block ? 0x38 : 0x3c; /* scales .5 and1 */
      for (unsigned j = 0; j < 32; ++j) w[j+2] = (unsigned char)(seed + r + j*3 + block*7);
    }
  }
#ifdef __riscv
  __asm__ volatile("fence rw, rw" ::: "memory");
#endif
  if (!cm_q8_matvec(output, weights, 33, 64, input)) return fail(6, 0, 1);
  for (unsigned r = 0; r < 33; ++r) {
    float expected = 0;
    for (unsigned block = 0; block < 2; ++block) {
      int sum = 0;
      for (unsigned j = 0; j < 32; ++j) {
        int code = (int)((seed + r + j*3 + block*7) & 255u);
        if (code >= 128) code -= 256;
        sum += code * codes[j%8];
      }
      expected += (float)sum * (block ? .5f : 1.0f);
    }
    if (cm_q8_bits(output[r]) != cm_q8_bits(expected))
      return fail(7, (int)cm_q8_bits(output[r]), (int)cm_q8_bits(expected));
  }
  q8_probe_result[4] = 33;
  q8_probe_result[5] = cm_q8_bits(output[0]);
#ifdef CM_Q8_RVV
  q8_probe_result[6] = 1;
#endif
  q8_probe_result[1] = 0x50415353u;
  return 0;
}
