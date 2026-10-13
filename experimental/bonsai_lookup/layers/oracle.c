/* Independent scalar reference: native PQ2 decode, exact group dot, ordered FP32.
 * Host correctness oracle only; never use its runtime as accelerator performance.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

static float half_to_float(uint16_t h) {
  uint32_t sign = (uint32_t)(h & 0x8000u) << 16;
  uint32_t exp = (h >> 10) & 31u, mant = h & 1023u, bits;
  if (exp == 0) {
    if (mant == 0)
      bits = sign;
    else {
      int shift = 0;
      while ((mant & 1024u) == 0) {
        mant <<= 1;
        ++shift;
      }
      bits = sign | (uint32_t)(113 - shift) << 23 | (mant & 1023u) << 13;
    }
  } else if (exp == 31)
    bits = sign | 0x7f800000u | mant << 13;
  else
    bits = sign | (exp + 112u) << 23 | mant << 13;
  float result;
  memcpy(&result, &bits, sizeof(result));
  return result;
}

void bonsai_layer_oracle(const uint8_t *native, const int8_t *a, const float *a_scale, uint32_t m,
                         uint32_t n, uint32_t k, int32_t *dots, float *outputs) {
  const size_t groups = k / 128;
  for (size_t token = 0; token < m; ++token) {
    for (size_t row = 0; row < n; ++row) {
      volatile float total = 0.0f;
      for (size_t group = 0; group < groups; ++group) {
        const uint8_t *block = native + (row * groups + group) * 34;
        int32_t dot          = 0;
        for (size_t lane = 0; lane < 128; ++lane) {
          int32_t weight = (int32_t)((block[2 + lane / 4] >> (2 * (lane % 4))) & 3) - 1;
          dot += (int32_t)a[token * k + group * 128 + lane] * weight;
        }
        dots[(token * n + row) * groups + group] = dot;
        float scale              = half_to_float((uint16_t)block[0] | (uint16_t)block[1] << 8);
        volatile float weighted  = (float)dot * scale;
        volatile float activated = weighted * a_scale[token * groups + group];
        total                    = total + activated;
      }
      outputs[token * n + row] = total;
    }
  }
}
