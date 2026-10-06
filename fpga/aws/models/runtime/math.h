/* SPDX-License-Identifier: Apache-2.0 */
#ifndef CORAL_MODEL_MATH_H
#define CORAL_MODEL_MATH_H
/* Internal FP32 inference math: round-to-nearest-even, gradual underflow,
 * and compiler contraction disabled. Return values are specified below;
 * IEEE exception flags and NaN payload preservation are not part of this API.
 * exp/log cover all binary32 inputs with IEEE-style special return values.
 * sqrt uses RV32F fsqrt.s on the target. */
float cm_exp(float x);
float cm_log(float x);
float cm_sqrt(float x);
/* Supports finite |x|<=4096, covering decoder angles through position 2047.
 * Signed zero is preserved in sine. Unsupported finite angles and infinities
 * return quiet NaNs in both outputs; NaN inputs are quieted. */
void cm_sincos(float x, float *s, float *c);
#endif
