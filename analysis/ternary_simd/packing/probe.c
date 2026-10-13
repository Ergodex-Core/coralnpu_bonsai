/* Candidate current-image probe. Never run without owner-approved scratch ranges.
 * No host loader, AFI operation, reset, DMA engine, or matrix instructions here.
 * Mailbox words 0..11 are inputs; 12..23 are outputs. Owner adapter must populate
 * input fields after ELF load and before launch. Unconfigured image exits safely.
 */
#include <stddef.h>
#include <stdint.h>

extern int bonsai_unpack(const volatile uint8_t *, volatile uint8_t *, uint32_t, uint32_t, uint32_t,
                         uint32_t);
extern void bonsai_copy_words(const volatile uint32_t *, volatile uint32_t *, size_t);
extern uint32_t bonsai_read_words(const volatile uint32_t *, size_t);

__attribute__((section(".mailbox"))) volatile uint32_t _ret[32] = {0};

static uint64_t cycles(void) {
  uint32_t hi, lo, again;
  do {
    __asm__ volatile("csrr %0,mcycleh" : "=r"(hi));
    __asm__ volatile("csrr %0,mcycle" : "=r"(lo));
    __asm__ volatile("csrr %0,mcycleh" : "=r"(again));
  } while (hi != again);
  return ((uint64_t)hi << 32) | lo;
}

static int valid_range(uint32_t address, uint32_t bytes) {
  return address >= 0xc0000000u && bytes > 0 && bytes <= 32u * 1024u * 1024u &&
         (uint64_t)address + bytes <= 0x100000000ull && address % 64 == 0;
}

static int fail(uint32_t code) {
  _ret[13] = code;
  _ret[22] = _ret[11];
  _ret[12] = 3;
  return (int)code;
}

int main(void) {
  /* magic,version,op,layout,order,N,K,src,src_bytes,dst,dst_capacity,cookie */
  if (_ret[0] != 0x42505132u || _ret[1] != 1)
    return fail(1);
  uint32_t op = _ret[2], layout = _ret[3], order = _ret[4];
  uint32_t n = _ret[5], k = _ret[6], src = _ret[7], input = _ret[8];
  uint32_t dst = _ret[9], capacity = _ret[10];
  _ret[12] = 1; /* Validating; no launch permission or ownership inference. */
  if (op > 3 || order > 1)
    return fail(2);
  uint64_t output = op == 2 ? input : 0;
  if (op == 3) {
    if (n == 0 || n > 2048 || k == 0 || k > 6144 || k % 128 || layout > 4)
      return fail(3);
    uint64_t groups = k / 128;
    uint64_t rows   = layout == 2 ? 16 : 32;
    uint64_t expected =
        layout <= 1 ? n * groups * (layout == 0 ? 34 : 64)
                    : ((n + rows - 1) / rows) * groups * (rows * (layout == 4 ? 26 : 32) + 64);
    if (expected != input)
      return fail(4);
    output = (uint64_t)n * groups * 130;
  }
  if (op && !valid_range(src, input))
    return fail(5);
  if ((op == 1 || op == 2) && input % 4)
    return fail(6);
  if (output &&
      (!valid_range(dst, capacity) || capacity < output ||
       ((uint64_t)src < (uint64_t)dst + capacity && (uint64_t)dst < (uint64_t)src + input)))
    return fail(7);
  uint32_t checksum = 0;
  int rc            = 0;
  __asm__ volatile("fence iorw,iorw" ::: "memory");
  uint64_t start = cycles();
  if (op == 1)
    checksum = bonsai_read_words((const volatile uint32_t *)(uintptr_t)src, input / 4);
  else if (op == 2)
    bonsai_copy_words((const volatile uint32_t *)(uintptr_t)src,
                      (volatile uint32_t *)(uintptr_t)dst, input / 4);
  else if (op == 3)
    rc = bonsai_unpack((const volatile uint8_t *)(uintptr_t)src, (volatile uint8_t *)(uintptr_t)dst,
                       n, k, layout, order);
  __asm__ volatile("fence iorw,iorw" ::: "memory");
  uint64_t end = cycles(), elapsed = end - start;
  _ret[13] = (uint32_t)rc;
  _ret[14] = (uint32_t)start;
  _ret[15] = start >> 32;
  _ret[16] = (uint32_t)end;
  _ret[17] = end >> 32;
  _ret[18] = (uint32_t)elapsed;
  _ret[19] = elapsed >> 32;
  _ret[20] = checksum;
  _ret[21] = (uint32_t)output;
  _ret[22] = _ret[11];
  _ret[23] = 25000000;
  _ret[12] = rc == 0 ? 2 : 3;
  return rc == 0 ? 0 : 8;
}
