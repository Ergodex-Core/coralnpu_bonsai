/* SPDX-License-Identifier: Apache-2.0 */
#include "generate.h"
static uint64_t cycles(void) {
#ifdef __riscv
  uint32_t lo,hi,again;
  do {
    __asm__ volatile("csrr %0, 0xb80" : "=r"(hi) :: "memory");
    __asm__ volatile("csrr %0, 0xb00" : "=r"(lo) :: "memory");
    __asm__ volatile("csrr %0, 0xb80" : "=r"(again) :: "memory");
  } while(hi!=again);
  return ((uint64_t)hi<<32)|lo;
#else
  return 0; /* Host references do not claim target cycle measurements. */
#endif
}
static uint32_t argmax(const float *x,uint32_t n) {
  uint32_t best=0;
  for(uint32_t i=1;i<n;++i)if(x[i]>x[best])best=i;
  return best;
}
int cm_generate(cm_state *s,const uint32_t *prompt,uint32_t prompt_count,
                uint32_t max_new_tokens,const uint32_t *eos,uint32_t eos_count,
                uint32_t *generated,uint32_t generated_capacity,
                float *logits,uint32_t logits_capacity,cm_generation_result *r) {
  if(!s || !s->h || !r || !prompt || !prompt_count || s->position ||
     prompt_count>s->capacity || max_new_tokens>s->capacity-prompt_count+1 ||
     eos_count>32 || (eos_count && !eos) || !logits ||
     (max_new_tokens && (!generated || generated_capacity<max_new_tokens)))return CM_BAD_REQUEST;
  uint32_t vocab=s->h->vocab,rows=max_new_tokens?max_new_tokens:1;
  if(logits_capacity/vocab<rows)return CM_BAD_REQUEST;
  for(uint32_t i=0;i<prompt_count;++i)if(prompt[i]>=vocab)return CM_BAD_TOKEN;
  for(uint32_t i=0;i<eos_count;++i)if(eos[i]>=vocab)return CM_BAD_TOKEN;
  r->completed_tokens=0; r->generated_count=0; r->last_argmax=0xffffffffu; r->stop_reason=CM_STOP_PREFILL;
  r->prefill_cycles=r->decode_cycles=r->total_cycles=r->first_token_cycles=0;
  uint64_t begin=cycles();
  for(uint32_t i=0;i<prompt_count;++i) {
    int rc=cm_step(s,prompt[i],logits,vocab);
    if(rc)return rc;
    ++r->completed_tokens;
  }
  uint64_t prefilled=cycles(); r->prefill_cycles=prefilled-begin;
  r->last_argmax=argmax(logits,vocab);
  r->first_token_cycles=cycles()-begin;
  for(uint32_t i=0;i<max_new_tokens;++i) {
    if(i) {
      int rc=cm_step(s,generated[i-1],logits+i*vocab,vocab);
      if(rc)return rc;
      ++r->completed_tokens;
      r->last_argmax=argmax(logits+i*vocab,vocab);
    }
    generated[i]=r->last_argmax; ++r->generated_count;
    r->stop_reason=CM_STOP_LENGTH;
    for(uint32_t e=0;e<eos_count;++e)
      if(eos[e]==r->last_argmax)r->stop_reason=CM_STOP_EOS;
    if(r->stop_reason==CM_STOP_EOS)break;
  }
  uint64_t end=cycles(); r->decode_cycles=end-prefilled; r->total_cycles=end-begin;
  return CM_OK;
}
