/* SPDX-License-Identifier: Apache-2.0 */
#ifndef CORAL_MODEL_DECODER_H
#define CORAL_MODEL_DECODER_H
#include "../model_format.h"
enum { CM_OK=0, CM_BAD_IMAGE=1, CM_BAD_CONFIG=2, CM_BAD_TENSOR=3,
       CM_NO_WORKSPACE=4, CM_BAD_TOKEN=5, CM_BAD_POSITION=6,
       CM_BAD_REQUEST=7, CM_NUMERIC_ERROR=8 };
enum { CM_TRACE_ATTN_NORM=1, CM_TRACE_Q=2, CM_TRACE_K=3, CM_TRACE_V=4,
       CM_TRACE_ATTN=5, CM_TRACE_FFN_NORM=6, CM_TRACE_GATE=7,
       CM_TRACE_UP=8, CM_TRACE_LAYER=9, CM_TRACE_LOGITS=10 };
typedef void (*cm_trace_fn)(unsigned stage, unsigned layer, unsigned position,
                            const float *values, unsigned count, void *context);
typedef struct {
  const cm_header *h;
  const unsigned char *image;
  const cm_tensor *directory;
  uint32_t capacity, position;
  float *x, *norm, *q, *k, *v, *att, *residual, *gate, *up, *scores;
  float *rope_inv, *keys, *values;
  float rope_magnitude;
  cm_trace_fn trace;
  void *trace_context;
} cm_state;
/* Returns zero on invalid/overflowing geometry. No allocations occur. */
uint32_t cm_workspace_bytes(const cm_header *h, uint32_t capacity);
int cm_init(cm_state *s, const void *image, uint32_t image_bytes,
            void *workspace, uint32_t workspace_bytes, uint32_t capacity);
int cm_step(cm_state *s, uint32_t token, float *logits, uint32_t logits_count);
float cm_weight(const unsigned char *image, const cm_tensor *t,
                uint32_t row, uint32_t col);
void cm_matvec(float *out, const unsigned char *image, const cm_tensor *t,
               const float *input);
void cm_rms(float *out, const float *input, const unsigned char *image,
            const cm_tensor *weight, uint32_t n, float eps);
void cm_softmax(float *x, uint32_t n);
#endif
