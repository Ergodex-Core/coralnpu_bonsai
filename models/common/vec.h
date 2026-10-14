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
// Small scalar math helpers for kernels (no libm on the device).
#ifndef MODELS_COMMON_VEC_H_
#define MODELS_COMMON_VEC_H_

#include <stdint.h>

static inline float f32_from_bits(uint32_t b) {
  union { uint32_t u; float f; } c; c.u = b; return c.f;
}
static inline uint32_t f32_bits(float f) {
  union { uint32_t u; float f; } c; c.f = f; return c.u;
}

// exp(x) for f32, |rel err| < 2e-7 on [-87, 88]: range reduction by ln2 and a
// degree-6 polynomial on [-ln2/2, ln2/2].
static inline float fast_exp(float x) {
  if (x > 88.0f) x = 88.0f;
  if (x < -87.0f) return 0.0f;
  const float log2e = 1.44269504f;
  float n = x * log2e;
  n = (float)(int)(n + (n >= 0.0f ? 0.5f : -0.5f));
  float r = x - n * 0.693145751953125f - n * 1.428606765330187e-06f;
  float p = 1.0f / 720.0f;
  p = p * r + 1.0f / 120.0f;
  p = p * r + 1.0f / 24.0f;
  p = p * r + 1.0f / 6.0f;
  p = p * r + 0.5f;
  p = p * r + 1.0f;
  p = p * r + 1.0f;
  return p * f32_from_bits((uint32_t)((int)n + 127) << 23);
}

// tanh via exp: tanh(z) = 1 - 2 / (1 + exp(2z)).
static inline float fast_tanh(float z) {
  if (z > 9.0f) return 1.0f;
  if (z < -9.0f) return -1.0f;
  return 1.0f - 2.0f / (1.0f + fast_exp(2.0f * z));
}

// Gemma's approximate GELU: 0.5 x (1 + tanh(sqrt(2/pi) (x + 0.044715 x^3))).
static inline float gelu_tanh(float x) {
  const float c = 0.7978845608f;
  return 0.5f * x * (1.0f + fast_tanh(c * (x + 0.044715f * x * x * x)));
}

static inline float fsqrt(float x) {
  float r;
  __asm__("fsqrt.s %0, %1" : "=f"(r) : "f"(x));
  return r;
}

#endif  // MODELS_COMMON_VEC_H_
