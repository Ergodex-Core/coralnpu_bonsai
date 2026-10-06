/* SPDX-License-Identifier: MIT
 * Q8_0 layout / scalar quantization / block dot derived from ggml llama.cpp
 * 8345f333951c661d166b00e6f9362e553768f292. See Q8_LICENSE.txt.
 * Profile: scalar reference roundf ties-away activation codes, binary16 RNE
 * scales, exact I32 block dot, ordered FP32 scale/multiply/add, FMA disabled.
 * RVV accelerates only the exact integer dot. It does not change quantization.
 */
#ifndef CM_Q8_0_H_
#define CM_Q8_0_H_
#include <stdint.h>
#ifdef CM_Q8_RVV
#if !defined(__riscv_vector)
#error CM_Q8_RVV requires a RISC-V vector target
#endif
#include <riscv_vector.h>
#endif
static inline uint32_t cm_q8_bits(float f) {
  union {
    float f;
    uint32_t u;
  } x = {f};
  return x.u;
}
static inline float cm_q8_float(uint32_t u) {
  union {
    uint32_t u;
    float f;
  } x = {u};
  return x.f;
}
/* Positive finite FP32 to binary16, round-to-nearest-even, integer only. */
static inline uint16_t cm_q8_half_rne(float f) {
  uint32_t u = cm_q8_bits(f), e = (u >> 23) & 255u, m = u & 0x7fffffu;
  if (e < 102u)
    return 0;
  if (e >= 143u)
    return 0x7c00u;
  uint32_t shift       = e < 113u ? 126u - e : 13u;
  uint32_t significand = e < 113u ? m | 0x800000u : m;
  uint32_t base        = e < 113u ? 0u : (e - 112u) << 10;
  uint32_t q = significand >> shift, rem = significand & ((1u << shift) - 1u);
  uint32_t halfway = 1u << (shift - 1u);
  return (uint16_t)(base + q + (rem > halfway || (rem == halfway && (q & 1u))));
}
static inline float cm_q8_scale(const unsigned char *p) {
  uint32_t u = p[0] | ((uint32_t)p[1] << 8);
  uint32_t s = (u & 0x8000u) << 16, e = (u >> 10) & 31u, m = u & 1023u;
  if (!e) {
    if (!m)
      return cm_q8_float(s);
    e = 113;
    while (!(m & 1024u)) {
      m <<= 1;
      --e;
    }
    return cm_q8_float(s | (e << 23) | ((m & 1023u) << 13));
  }
  return cm_q8_float(s | ((e == 31 ? 255u : e + 112u) << 23) | (m << 13));
}
static inline int cm_q8_quant32(unsigned char out[34], const float *x) {
  float amax = 0;
  for (unsigned j = 0; j < 32; ++j) {
    uint32_t b = cm_q8_bits(x[j]) & 0x7fffffffu;
    if (b >= 0x7f800000u)
      return 0;
    float a = cm_q8_float(b);
    if (a > amax)
      amax = a;
  }
  float d = amax / 127.0f, inverse = d ? 1.0f / d : 0;
  uint16_t h = cm_q8_half_rne(d);
  /* Reject undefined/nonfinite upstream domains before float-to-int casts. */
  if (h >= 0x7c00u || (cm_q8_bits(inverse) & 0x7fffffffu) >= 0x7f800000u)
    return 0;
  out[0] = (unsigned char)h;
  out[1] = (unsigned char)(h >> 8);
  for (unsigned j = 0; j < 32; ++j) {
    float v        = x[j] * inverse;
    int q          = (int)v; /* RTZ plus explicit half-away adjustment equals roundf. */
    float fraction = v - (float)q;
    if (fraction >= 0.5f)
      ++q;
    if (fraction <= -0.5f)
      --q;
    if (q < -127 || q > 127)
      return 0;
    out[j + 2] = (unsigned char)(q & 255);
  }
  return 1;
}
static inline int32_t cm_q8_dot32(const unsigned char *a, const unsigned char *b) {
#ifdef CM_Q8_RVV
  /* VLEN128: i8m2 holds all32 codes; I16 products widened into I32 sum.
   * In particular (-128)*(-128) stays16384: no pairwise I16 add overflow. */
  vint8m2_t x        = __riscv_vle8_v_i8m2((const int8_t *)a, 32);
  vint8m2_t y        = __riscv_vle8_v_i8m2((const int8_t *)b, 32);
  vint16m4_t product = __riscv_vwmul_vv_i16m4(x, y, 32);
  vint32m1_t zero    = __riscv_vmv_v_x_i32m1(0, 32);
  vint32m1_t sum     = __riscv_vwredsum_vs_i16m4_i32m1(product, zero, 32);
  return __riscv_vmv_x_s_i32m1_i32(sum);
#else
  int32_t sum = 0;
  for (unsigned j = 0; j < 32; ++j) {
    int x = a[j] < 128 ? a[j] : (int)a[j] - 256;
    int y = b[j] < 128 ? b[j] : (int)b[j] - 256;
    sum += x * y;
  }
  return sum;
#endif
}
static inline int cm_q8_matvec(float *out, const unsigned char *weights, uint32_t rows,
                               uint32_t cols, const float *x) {
  if (!cols || cols % 32)
    return 0;
  uint32_t stride = cols / 32 * 34;
  /* Bounded local storage:32 accumulators + one activation block. RVV loads
   * packed weights directly into registers from the external window, avoiding
   * scalar byte-copy traffic. Reuse activation quantization across32 rows. */
  for (uint32_t first = 0; first < rows; first += 32) {
    uint32_t n = rows - first;
    if (n > 32)
      n = 32;
    float sums[32] = {0};
    unsigned char activation[34];
    for (uint32_t col = 0; col < cols; col += 32) {
      if (!cm_q8_quant32(activation, x + col))
        return 0;
      float ds = cm_q8_scale(activation);
      for (uint32_t r = 0; r < n; ++r) {
        const unsigned char *w = weights + (first + r) * stride + col / 32 * 34;
        float scale            = cm_q8_scale(w) * ds;
        float value            = (float)cm_q8_dot32(w + 2, activation + 2) * scale;
        sums[r] += value;
      }
    }
    for (uint32_t r = 0; r < n; ++r)
      out[first + r] = sums[r];
  }
  return 1;
}
#endif
