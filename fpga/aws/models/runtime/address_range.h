/* SPDX-License-Identifier: Apache-2.0 */
#ifndef CM_ADDRESS_RANGE_H_
#define CM_ADDRESS_RANGE_H_
#include "mailbox.h"
static inline int cm_address_range(uint32_t p, uint32_t n) {
  return !(p & 3u) && p >= CM_MODEL_MIN &&
         n && (uint64_t)p + (uint64_t)n <= CM_DDR_END;
}
static inline int cm_ranges_overlap(uint32_t a, uint32_t na, uint32_t b, uint32_t nb) {
  return na && nb && (uint64_t)a < (uint64_t)b + nb && (uint64_t)b < (uint64_t)a + na;
}
#endif
