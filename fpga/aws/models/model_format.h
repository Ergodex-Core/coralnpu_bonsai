#ifndef CORAL_MODEL_FORMAT_H_
#define CORAL_MODEL_FORMAT_H_
/* CORALM01 v1: all fields little-endian; offsets relative to image start. */
#include <stdint.h>
#define CM_MAGIC        "CORALM01"
#define CM_VERSION      1u
#define CM_VERSION_Q8   2u
#define CM_GLOBAL_LAYER UINT32_C(0xffffffff)
enum { CM_F32 = 1, CM_BF16 = 2, CM_PQ2_0 = 3, CM_Q8_0 = 4 };
enum { CM_TIED_EMBEDDINGS = 1, CM_QK_NORM = 2, CM_YARN = 4 };
enum {
  CM_EMBEDDING     = 1,
  CM_FINAL_NORM    = 2,
  CM_OUTPUT        = 3,
  CM_INPUT_NORM    = 10,
  CM_Q             = 11,
  CM_K             = 12,
  CM_V             = 13,
  CM_O             = 14,
  CM_Q_NORM        = 15,
  CM_K_NORM        = 16,
  CM_POSTATTN_NORM = 17,
  CM_GATE          = 18,
  CM_UP            = 19,
  CM_DOWN          = 20
};
typedef struct {
  char magic[8];
  uint32_t version, header_bytes, file_bytes, tensor_count, dir_offset;
  uint32_t dim, hidden_dim, n_layers, n_heads, n_kv_heads, head_dim, vocab;
  uint32_t max_seq, flags;
  float rope_theta, rms_eps, rope_factor, rope_original_context;
  float yarn_beta_fast, yarn_beta_slow, yarn_attention_factor;
  uint32_t reserved[9];
} cm_header;
typedef struct {
  uint32_t role, layer, encoding, rows, cols, offset, bytes, reserved;
} cm_tensor;
#if defined(__cplusplus)
static_assert(sizeof(cm_header) == 128, "cm_header ABI");
static_assert(sizeof(cm_tensor) == 32, "cm_tensor ABI");
#else
_Static_assert(sizeof(cm_header) == 128, "cm_header ABI");
_Static_assert(sizeof(cm_tensor) == 32, "cm_tensor ABI");
#endif
#endif
