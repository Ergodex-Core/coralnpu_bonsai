/* SPDX-License-Identifier: Apache-2.0 */
#ifndef CORAL_MODEL_GENERATE_H
#define CORAL_MODEL_GENERATE_H
#include "decoder.h"
enum { CM_STOP_PREFILL = 0, CM_STOP_LENGTH = 1, CM_STOP_EOS = 2 };
typedef struct {
  uint32_t completed_tokens, generated_count, last_argmax, stop_reason;
  uint64_t prefill_cycles, decode_cycles, total_cycles, first_token_cycles;
} cm_generation_result;
/* Computes the entire prompt on Coral, then greedy autoregressive steps using
 * the SAME KV state. Logits are [max(1,max_new_tokens),vocab]. The final emitted
 * token is not processed again because no next prediction is requested. */
int cm_generate(cm_state *state, const uint32_t *prompt, uint32_t prompt_count,
                uint32_t max_new_tokens, const uint32_t *eos, uint32_t eos_count,
                uint32_t *generated, uint32_t generated_capacity, float *logits,
                uint32_t logits_capacity, cm_generation_result *result);
#endif
