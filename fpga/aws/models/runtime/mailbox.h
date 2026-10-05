/* SPDX-License-Identifier: Apache-2.0 */
#ifndef CORAL_MODEL_MAILBOX_H
#define CORAL_MODEL_MAILBOX_H
#include <stdint.h>
#define CM_MAILBOX_ADDRESS 0x10000u
#define CM_MAILBOX_MAGIC   0x434d5231u
#define CM_DDR_BASE        0x20000000u
#define CM_DDR_END         0xa0000000u
#define CM_MODEL_MIN       0x21000000u
/* Host writes all inputs while reset is asserted, then releases reset.
 * State/completed/error are firmware outputs, observed only after halt or fence.
 * Running status is advisory; the host never modifies a running request. */
typedef struct {
  uint32_t magic, abi_version, state, error;
  uint32_t model_addr, model_bytes, workspace_addr, workspace_bytes;
  uint32_t tokens_addr, token_count, logits_addr, logits_capacity;
  uint32_t completed_tokens, last_argmax, max_seq_capacity, max_new_tokens;
  uint32_t eos_count, eos_addr, generated_addr, generated_capacity;
  uint32_t generated_count, stop_reason;
  uint32_t prefill_cycles_lo, prefill_cycles_hi, decode_cycles_lo, decode_cycles_hi;
  uint32_t total_cycles_lo, total_cycles_hi, first_token_cycles_lo, first_token_cycles_hi;
  uint32_t reserved[2];
} cm_mailbox;
enum { CM_IDLE = 0, CM_RUNNING = 1, CM_DONE = 2, CM_ERROR = 3 };
#endif
