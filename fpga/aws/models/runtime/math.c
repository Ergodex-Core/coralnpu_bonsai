/* SPDX-License-Identifier: Apache-2.0 */
#include "math.h"

#include <stdint.h>
/* All intermediate arithmetic is FP32, without libc or double soft-float.
 * Tested approximation budget is documented in test_runtime.py. */
float cm_exp(float x) {
  union {
    uint32_t u;
    float f;
  } scale;
  if (x != x)
    return x + x; /* Quiet signaling NaNs before range reduction. */
  if (x < -104.0f)
    return 0.0f;
  if (x > 89.0f) {
    scale.u = 0x7f800000u;
    return scale.f;
  }
  int k   = (int)(x * 1.4426950408889634f + (x >= 0 ? .5f : -.5f));
  float r = (x - (float)k * .693145751953125f) - (float)k * 1.428606765330187e-6f;
  float p =
      1.0f + r * (1.0f + r * (.5f + r * (.1666666667f +
                                         r * (.04166666667f +
                                              r * (.008333333333f +
                                                   r * (.001388888889f + r * .0001984126984f))))));
  /* Keep the exponent-field construction normal and scale only once into
   * the subnormal/overflow range. All intermediate arithmetic stays FP32. */
  if (k < -126) {
    scale.u = (uint32_t)(k + 152) << 23;
    return (p * scale.f) * 0x1p-25f;
  }
  if (k > 127) {
    scale.u = (uint32_t)(k + 126) << 23;
    return (p * scale.f) * 2.0f;
  }
  scale.u = (uint32_t)(k + 127) << 23;
  return p * scale.f;
}
float cm_sqrt(float x) {
#ifdef __riscv
  float out;
  __asm__("fsqrt.s %0, %1" : "=f"(out) : "f"(x));
  return out;
#else
  return __builtin_sqrtf(x);
#endif
}
float cm_log(float x) {
  union {
    uint32_t u;
    float f;
  } v   = {0};
  v.f = x;
  uint32_t magnitude = v.u & 0x7fffffffu;
  if (magnitude > 0x7f800000u)
    return x + x; /* Quiet a NaN without interpreting its exponent. */
  if (!magnitude) {
    v.u = 0xff800000u;
    return v.f; /* log(+0) and log(-0) are negative infinity. */
  }
  if (v.u & 0x80000000u) {
    v.u = 0x7fc00000u;
    return v.f; /* A negative nonzero argument is outside the real domain. */
  }
  if (magnitude == 0x7f800000u)
    return x;
  int correction = 0;
  if (magnitude < 0x00800000u) {
    v.f = x * 0x1p23f; /* Exact normalization, including the least subnormal. */
    correction = -23;
  }
  int e = (int)(v.u >> 23) - 127 + correction;
  v.u   = (v.u & 0x7fffffu) | 0x3f800000u;
  /* Reduce mantissa to [sqrt(.5),sqrt(2)] for the atanh series. */
  if (v.f > 1.41421356237f) {
    v.f *= .5f;
    ++e;
  }
  float y = (v.f - 1.0f) / (v.f + 1.0f), z = y * y;
  return 2 * y * (1 + z * (1.0f / 3 + z * (1.0f / 5 + z * (1.0f / 7 + z * (1.0f / 9 + z / 11))))) +
         (float)e * .69314718056f;
}
void cm_sincos(float x, float *s, float *c) {
  union {
    uint32_t u;
    float f;
  } argument;
  argument.f = x;
  if (!(argument.u & 0x7fffffffu)) {
    *s = x;
    *c = 1.0f;
    return;
  }
  if ((argument.u & 0x7fffffffu) > 0x45800000u) {
    /* The decoder uses |x|<=2047. Reject unsupported finite angles and
     * infinities before conversion to int; propagate NaNs as quiet NaNs. */
    if ((argument.u & 0x7fffffffu) > 0x7f800000u)
      argument.f = x + x;
    else
      argument.u = 0x7fc00000u;
    *s = argument.f;
    *c = argument.f;
    return;
  }
  /* The high pi/2 term has 12 significant bits, so q*high is exact
   * in FP32 for supported |x|<=4096 (|q|<=2608). The low term then
   * corrects the exact product without losing the reduced angle. */
  int q    = (int)(x * .6366197723675813f + (x >= 0 ? .5f : -.5f));
  float r  = (x - (float)q * 1.57080078125f) + (float)q * 4.4544551033807687e-6f;
  float z  = r * r;
  float sn = r * (1 + z * (-1.0f / 6 + z * (1.0f / 120 + z * (-1.0f / 5040 + z / 362880))));
  float cs = 1 + z * (-.5f + z * (1.0f / 24 + z * (-1.0f / 720 + z / 40320)));
  switch ((unsigned)q & 3u) {
    case 0:
      *s = sn;
      *c = cs;
      break;
    case 1:
      *s = cs;
      *c = -sn;
      break;
    case 2:
      *s = -sn;
      *c = -cs;
      break;
    default:
      *s = -cs;
      *c = sn;
      break;
  }
}
