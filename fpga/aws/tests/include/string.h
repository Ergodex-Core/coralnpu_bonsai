/* Minimal freestanding declarations used by the unmodified upstream examples. */
#pragma once
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
void *memcpy(void *, const void *, size_t);
void *memset(void *, int, size_t);
#ifdef __cplusplus
}
#endif
