// SPDX-License-Identifier: Apache-2.0
// Freestanding runtime for the four basic upstream examples only.
#include <string.h>
extern "C" void *memcpy(void *dst, const void *src, size_t n) {
  auto *d       = static_cast<unsigned char *>(dst);
  const auto *s = static_cast<const unsigned char *>(src);
  for (size_t i = 0; i < n; ++i)
    d[i] = s[i];
  return dst;
}
extern "C" void *memset(void *dst, int value, size_t n) {
  auto *d = static_cast<unsigned char *>(dst);
  for (size_t i = 0; i < n; ++i)
    d[i] = static_cast<unsigned char>(value);
  return dst;
}
// These examples have no constructors, registered finalizers, or heap use.
extern "C" void __cxa_finalize() {}
extern "C" __attribute__((naked)) void coralnpu_exception_handler() {
  asm volatile("ebreak\n1: j 1b");
}
