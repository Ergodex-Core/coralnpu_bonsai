"""Exact Q8 primitives: NumPy oracle plus pinned ggml scalar C reference.

The reference quantizer equations are from llama.cpp
8345f333951c661d166b00e6f9362e553768f292 (MIT; Q8_LICENSE.txt).
No full-model or target-execution claim follows from these CPU tests.
"""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest
import numpy as np

HERE = Path(__file__).resolve().parent
FIXTURE = r'''
#include "q8_0.h"
#include <math.h>
int quant(unsigned char *y, const float *x) { return cm_q8_quant32(y,x); }
uint16_t half_bits(float x) { return cm_q8_half_rne(x); }
int dot(const unsigned char *x, const unsigned char *y) { return cm_q8_dot32(x,y); }
int mat(float *y, const unsigned char *w, unsigned rows, unsigned cols, const float *x) {
  return cm_q8_matvec(y,w,rows,cols,x);
}
/* ggml quantize_row_q8_0_ref equations, one32-value block; host binary16
 * conversion independent of the target's integer implementation. */
void reference(unsigned char *out, const float *x) {
  float amax = 0;
  for (int j=0;j<32;++j) amax = fmaxf(amax, fabsf(x[j]));
  float d = amax / 127.0f, id = d ? 1.0f/d : 0.0f;
  union { _Float16 f; uint16_t u; } h = {(_Float16)d};
  out[0] = h.u; out[1] = h.u >> 8;
  for (int j=0;j<32;++j) out[j+2] = (unsigned char)((int)roundf(x[j]*id)&255);
}
'''


class Q8Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        p = Path(cls.tmp.name)
        (p / 'fixture.c').write_text(FIXTURE)
        subprocess.run([
            'cc', '-shared', '-fPIC', '-std=c11', '-O2', '-Wall', '-Wextra',
            '-Werror', '-ffp-contract=off', '-fno-fast-math',
            '-iquote' + str(HERE),
            str(p / 'fixture.c'), '-o',
            str(p / 'fixture.so'), '-lm'
        ],
                       check=True)
        cls.lib = C.CDLL(str(p / 'fixture.so'))
        cls.lib.quant.argtypes = cls.lib.reference.argtypes = [
            C.c_void_p, C.c_void_p
        ]
        cls.lib.half_bits.argtypes = [C.c_float]
        cls.lib.half_bits.restype = C.c_uint16
        cls.lib.dot.argtypes = [C.c_void_p, C.c_void_p]
        cls.lib.mat.argtypes = [
            C.c_void_p, C.c_void_p, C.c_uint, C.c_uint, C.c_void_p
        ]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_half_rounding(self):
        # Every adjacent positive finite-half midpoint and both adjacent FP32s.
        h = np.arange(
            0x7bff, dtype=np.uint16
        ).view(np.float16).astype(np.float32)
        hi = np.arange(
            1, 0x7c00, dtype=np.uint16
        ).view(np.float16).astype(np.float32)
        mid = (h + hi) * np.float32(.5)
        values = np.concatenate([
            mid,
            np.nextafter(mid, np.float32(0)),
            np.nextafter(mid, np.float32(np.inf)),
            np.array([0, 65504, 65519, 65520], np.float32)
        ])
        with np.errstate(over='ignore'):
            expected = values.astype(np.float16).view(np.uint16)
        actual = np.array([self.lib.half_bits(float(v)) for v in values],
                          np.uint16)
        np.testing.assert_array_equal(actual, expected)

    def test_activation_bytes(self):
        rng = np.random.default_rng(812)
        values = rng.normal(size=(2048, 32)).astype(np.float32)
        values *= np.exp2(rng.integers(-10, 16,
                                       size=(2048, 1))).astype(np.float32)
        ties = np.array([127, -127, .5, -.5, 1.5, -1.5, 126.5, -126.5] * 4,
                        np.float32)
        values = np.concatenate([
            values, ties[None, :],
            np.zeros((1, 32), np.float32)
        ])
        for x in values:
            a, b = np.empty(34, np.uint8), np.empty(34, np.uint8)
            self.assertEqual(self.lib.quant(a.ctypes.data, x.ctypes.data), 1)
            self.lib.reference(b.ctypes.data, x.ctypes.data)
            np.testing.assert_array_equal(a, b)

    def test_invalid_domain(self):
        for value in [float('nan'), float('inf'), -float('inf'), 1e20, 1e-37]:
            x = np.full(32, value, np.float32)
            out = np.empty(34, np.uint8)
            self.assertEqual(self.lib.quant(out.ctypes.data, x.ctypes.data), 0)

    def test_signed_dot(self):
        rng = np.random.default_rng(712)
        for _ in range(1000):
            a, b = rng.integers(
                -128, 128, size=(2, 32), dtype=np.int16
            ).astype(np.int8)
            expected = int(np.sum(a.astype(np.int32) * b.astype(np.int32)))
            self.assertEqual(
                self.lib.dot(a.ctypes.data, b.ctypes.data), expected
            )
        a = np.full(32, -128, np.int8)
        self.assertEqual(self.lib.dot(a.ctypes.data, a.ctypes.data), 524288)

    def test_matrix_tails_and_block_order(self):
        rng = np.random.default_rng(921)
        for rows, cols in [(1, 32), (31, 64), (32, 128), (33, 3072)]:
            n = cols // 32
            w = np.empty((rows, n, 34), np.uint8)
            scales = rng.uniform(-1, 1, size=(rows, n)).astype(np.float16)
            w[:, :, :2] = scales.view(np.uint8).reshape(rows, n, 2)
            w[:, :, 2:] = rng.integers(
                0, 256, size=(rows, n, 32), dtype=np.uint8
            )
            x = rng.normal(size=cols).astype(np.float32)
            a = np.empty((n, 34), np.uint8)
            for k in range(n):
                self.lib.reference(a[k].ctypes.data, x[k * 32:].ctypes.data)
            codes = a[:, 2:].copy().view(np.int8).astype(np.int32)
            ws = scales.astype(np.float32)
            ds = a[:, :2].copy().view(np.float16).reshape(n).astype(np.float32)
            sums = np.sum(
                w[:, :, 2:].copy().view(np.int8).astype(np.int32) * codes,
                axis=2,
                dtype=np.int32
            )
            terms = sums.astype(np.float32) * (ws * ds)
            expected = np.add.accumulate(
                terms, axis=1, dtype=np.float32
            )[:, -1]
            out = np.empty(rows, np.float32)
            self.assertEqual(
                self.lib.mat(
                    out.ctypes.data, w.ctypes.data, rows, cols, x.ctypes.data
                ), 1
            )
            np.testing.assert_array_equal(
                out.view(np.uint32), expected.view(np.uint32)
            )


if __name__ == '__main__': unittest.main()
