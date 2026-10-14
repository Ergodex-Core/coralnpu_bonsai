/* SPDX-License-Identifier: Apache-2.0 */
/* LLVM 18 needs flags when ._init is first created. Keep upstream CRT intact. */
.section ._init, "ax", @progbits
#include "toolchain/crt/coralnpu_start.S"
