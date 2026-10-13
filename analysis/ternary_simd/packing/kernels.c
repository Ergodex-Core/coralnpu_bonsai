/* Portable scalar kernels for the owner-coordinated current-image benchmark.
 * No MMIO, lifecycle, address allocation, or hardware driver is included.
 * The caller validates file/header hashes and buffer ownership before timing.
 * Timings include scalar decode/load/store overhead, not peak HBM bandwidth.
 */
#include <stddef.h>
#include <stdint.h>

/* layout IDs match layouts.py; src begins AFTER the 128-byte header.
 * Output: native row/group order, 128 signed bytes then two unchanged scale bytes.
 * Returns 0 on success; -1 on shape/layout error; -2 on invalid radix-3 encoding.
 * Callers provide the lengths from the validated header and prevent overlap.
 */
int bonsai_unpack(const volatile uint8_t *src, volatile uint8_t *dst, uint32_t n, uint32_t k,
                  uint32_t layout, uint32_t stream_order) {
  if (n == 0 || k == 0 || k % 128 || layout > 4)
    return -1;
  const size_t groups     = k / 128;
  const size_t rows       = layout == 2 ? 16 : 32;
  const size_t code_bytes = layout == 4 ? 26 : 32;
  size_t iterations =
      stream_order && layout >= 2 ? ((n + rows - 1) / rows) * rows * groups : n * groups;
  for (size_t index = 0; index < iterations; ++index) {
    size_t row = index / groups, group = index % groups;
    if (stream_order && layout >= 2) {
      row   = (index / (rows * groups)) * rows + index % rows;
      group = (index / rows) % groups;
    }
    if (row >= n)
      continue;
    size_t code, scale;
    if (layout <= 1) {
      scale = (row * groups + group) * (layout == 0 ? 34 : 64);
      code  = scale + 2;
    } else {
      size_t slab = ((row / rows) * groups + group) * (rows * code_bytes + 64);
      code        = slab + (row % rows) * code_bytes;
      scale       = slab + rows * code_bytes + (row % rows) * 2;
    }
    size_t target = (row * groups + group) * 130;
    if (layout == 4) {
      for (size_t byte = 0; byte < 26; ++byte) {
        uint32_t v = src[code + byte];
        if (v >= 243)
          return -2;
        for (size_t j = 0; j < 5; ++j) {
          uint32_t trit = v % 3;
          v /= 3;
          size_t lane = byte * 5 + j;
          if (lane < 128)
            dst[target + lane] = (uint8_t)((int)trit - 1);
          else if (trit != 1)
            return -2;
        }
      }
    } else {
      for (size_t byte = 0; byte < 32; ++byte) {
        uint32_t v = src[code + byte];
        for (size_t j = 0; j < 4; ++j)
          dst[target + byte * 4 + j] = (uint8_t)((int)((v >> (j * 2)) & 3) - 1);
      }
    }
    dst[target + 128] = src[scale];
    dst[target + 129] = src[scale + 1];
  }
  return 0;
}

/* Aligned read/write payload benchmark; compare the ENTIRE output after timing.
 * Each copied word transfers 4 read bytes and 4 write bytes. */
void bonsai_copy_words(const volatile uint32_t *src, volatile uint32_t *dst, size_t words) {
  for (size_t i = 0; i < words; ++i)
    dst[i] = src[i];
}

/* Four accumulators reduce dependence. This is read+checksum, not pure DMA. */
uint32_t bonsai_read_words(const volatile uint32_t *src, size_t words) {
  uint32_t a = 0, b = 0, c = 0, d = 0;
  size_t i = 0;
  for (; i + 3 < words; i += 4) {
    a += src[i];
    b += src[i + 1];
    c += src[i + 2];
    d += src[i + 3];
  }
  for (; i < words; ++i)
    a += src[i];
  return a + b + c + d;
}
