/* SPDX-License-Identifier: Apache-2.0 */
#include <stddef.h>

#include "generate.h"
#include "mailbox.h"
volatile cm_mailbox coral_mailbox __attribute__((section(".mailbox"), aligned(64)));
static cm_state state;
static void fence(void) { __asm__ volatile("fence rw, rw" ::: "memory"); }
static int fail(int error) {
  coral_mailbox.error = (uint32_t)error;
  fence();
  coral_mailbox.state = CM_ERROR;
  fence();
  return error;
}
static int range(uint32_t p, uint32_t n) {
  return !(p & 3u) && p >= CM_MODEL_MIN && p < CM_DDR_END && n && n <= CM_DDR_END - p;
}
int main(void) {
  volatile cm_mailbox *m = &coral_mailbox;
  if (m->magic != CM_MAILBOX_MAGIC || m->abi_version != 2 || m->reserved[0] || m->reserved[1])
    return fail(CM_BAD_REQUEST);
  m->completed_tokens  = 0;
  m->generated_count   = 0;
  m->error             = 0;
  m->last_argmax       = 0xffffffffu;
  m->stop_reason       = 0;
  m->prefill_cycles_lo = m->prefill_cycles_hi = m->decode_cycles_lo = m->decode_cycles_hi = 0;
  m->total_cycles_lo = m->total_cycles_hi = m->first_token_cycles_lo = m->first_token_cycles_hi = 0;
  m->state = CM_RUNNING;
  fence();
  if (!m->max_seq_capacity || m->max_seq_capacity > 2048 || !m->token_count ||
      m->token_count > m->max_seq_capacity ||
      m->max_new_tokens > m->max_seq_capacity - m->token_count + 1 || m->eos_count > 32 ||
      m->generated_capacity > 2048 || m->logits_capacity > 0x1fffffffu ||
      m->generated_capacity < m->max_new_tokens)
    return fail(CM_BAD_REQUEST);
  uint32_t addr[6] = {m->model_addr,  m->workspace_addr, m->tokens_addr,
                      m->logits_addr, m->eos_addr,       m->generated_addr};
  uint32_t size[6] = {m->model_bytes,         m->workspace_bytes, m->token_count * 4,
                      m->logits_capacity * 4, m->eos_count * 4,   m->generated_capacity * 4};
  for (unsigned i = 0; i < 6; ++i) {
    if (i >= 4 && !size[i])
      continue;
    if (!range(addr[i], size[i]))
      return fail(CM_BAD_REQUEST);
    for (unsigned j = 0; j < i; ++j)
      if (size[j] && addr[i] < addr[j] + size[j] && addr[j] < addr[i] + size[i])
        return fail(CM_BAD_REQUEST);
  }
  int rc = cm_init(&state, (const void *)(uintptr_t)m->model_addr, m->model_bytes,
                   (void *)(uintptr_t)m->workspace_addr, m->workspace_bytes, m->max_seq_capacity);
  if (rc)
    return fail(rc);
  cm_generation_result result;
  rc = cm_generate(&state, (const uint32_t *)(uintptr_t)m->tokens_addr, m->token_count,
                   m->max_new_tokens, (const uint32_t *)(uintptr_t)m->eos_addr, m->eos_count,
                   (uint32_t *)(uintptr_t)m->generated_addr, m->generated_capacity,
                   (float *)(uintptr_t)m->logits_addr, m->logits_capacity, &result);
  if (rc)
    return fail(rc);
  m->completed_tokens = result.completed_tokens;
  m->generated_count  = result.generated_count;
  m->last_argmax      = result.last_argmax;
  m->stop_reason      = result.stop_reason;
#define STORE_CYCLES(name)                        \
  do {                                            \
    m->name##_lo = (uint32_t)result.name;         \
    m->name##_hi = (uint32_t)(result.name >> 32); \
  } while (0)
  STORE_CYCLES(prefill_cycles);
  STORE_CYCLES(decode_cycles);
  STORE_CYCLES(total_cycles);
  STORE_CYCLES(first_token_cycles);
#undef STORE_CYCLES
  fence();
  m->state = CM_DONE;
  fence();
  return 0;
}
void *memcpy(void *dst, const void *src, size_t n) {
  unsigned char *d       = dst;
  const unsigned char *s = src;
  for (size_t i = 0; i < n; ++i)
    d[i] = s[i];
  return dst;
}
void *memset(void *dst, int value, size_t n) {
  unsigned char *d = dst;
  for (size_t i = 0; i < n; ++i)
    d[i] = (unsigned char)value;
  return dst;
}
void __cxa_finalize(void) {}
__attribute__((naked)) void coralnpu_exception_handler(void) {
  __asm__ volatile("ebreak\n1: j 1b");
}
