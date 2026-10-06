#!/usr/bin/env python3
"""Independent host regressions for FP32 elementary-function domains/accuracy.

Decimal (90 digits) supplies exp/log goldens and independent trig spot checks;
Python FP64 math supplies the dense trig sweep, never cm_* or a C float oracle.
Normal exp keeps its documented 8e-7 relative budget (also <=8 FP32 ULP).
Subnormal exp allows one minimum-subnormal step, where relative error is
ill-conditioned. Log permits three output ULP across the exponent range and
retains the original 7e-7 absolute budget on [0.01,10]. Bounded sin/cos must
have absolute error <1e-6, including zeros where relative error is undefined.
These GCC/Clang CPU checks do not qualify an LLVM/RV32 build or a full model.
"""
import ctypes as C
from decimal import Decimal, localcontext
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
PI = Decimal(
    '3.141592653589793238462643383279502884197169399375105820974944592307816406286208998628'
)


def fp32(value):
    try:
        return struct.unpack('<f', struct.pack('<f', value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def bits(value):
    return struct.unpack('<I', struct.pack('<f', value))[0]


def from_bits(value):
    return struct.unpack('<f', struct.pack('<I', value))[0]


def neighbors(value):
    """Immediate binary32 neighbors, including both sides of signed zero."""
    value = fp32(value)
    raw = bits(value)
    if value == 0:
        return (-from_bits(1), value, from_bits(1))
    return tuple(from_bits(raw + delta) for delta in (-1, 0, 1))


def ulps(actual, expected):

    def ordered(value):
        raw = bits(value)
        return 0x80000000 - (
            raw & 0x7fffffff
        ) if raw >> 31 else 0x80000000 + raw

    return abs(ordered(actual) - ordered(expected))


def decimal_golden(function, value):
    with localcontext() as context:
        context.prec = 90
        return float(getattr(Decimal.from_float(value), function)())


def decimal_sincos(value):
    """Direct Taylor sums after high-precision full-turn reduction."""
    with localcontext() as context:
        context.prec = 90
        x = Decimal.from_float(value) % (2 * PI)
        if x > PI:
            x -= 2 * PI
        if x < -PI:
            x += 2 * PI
        square = x * x
        sn = sine_term = x
        cs = cosine_term = Decimal(1)
        for k in range(1, 80):
            sine_term *= -square / ((2 * k) * (2 * k + 1))
            cosine_term *= -square / ((2 * k - 1) * (2 * k))
            sn += sine_term
            cs += cosine_term
            if abs(sine_term) + abs(cosine_term) < Decimal('1e-70'):
                break
        else:
            raise AssertionError('Decimal trig series did not converge')
        return float(sn), float(cs)


class MathNumericsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        compiler = os.environ.get('CC') or shutil.which('gcc') or shutil.which(
            'clang'
        )
        if not compiler:
            raise RuntimeError(
                'GCC or Clang is required for native math regressions'
            )
        cls.tmp = tempfile.TemporaryDirectory(prefix='coral-math-regression-')
        cls.addClassCleanup(cls.tmp.cleanup)
        library = Path(cls.tmp.name) / 'math.so'
        subprocess.run([
            compiler, '-shared', '-fPIC', '-O2', '-std=c11', '-Wall',
            '-Wextra', '-Werror', '-fno-fast-math', '-ffp-contract=off',
            str(HERE / 'math.c'), '-o',
            str(library), '-lm'
        ],
                       check=True,
                       capture_output=True,
                       text=True)
        cls.lib = C.CDLL(str(library))
        for name in ('cm_exp', 'cm_log'):
            function = getattr(cls.lib, name)
            function.argtypes = [C.c_float]
            function.restype = C.c_float
        cls.lib.cm_sincos.argtypes = [
            C.c_float, C.POINTER(C.c_float),
            C.POINTER(C.c_float)
        ]
        cls.lib.cm_sincos.restype = None

    def sincos(self, value):
        sn, cs = C.c_float(), C.c_float()
        self.lib.cm_sincos(value, C.byref(sn), C.byref(cs))
        return sn.value, cs.value

    def test_exp_special_values(self):
        for value in (0.0, -0.0):
            with self.subTest(value=value):
                self.assertEqual(bits(self.lib.cm_exp(value)), bits(1.0))
        self.assertTrue(math.isnan(self.lib.cm_exp(math.nan)))
        self.assertEqual(self.lib.cm_exp(math.inf), math.inf)
        self.assertEqual(bits(self.lib.cm_exp(-math.inf)), bits(0.0))

    def test_exp_gradual_underflow(self):
        values = [from_bits(0xff7fffff), -1000.0, -104.0]
        for value in (-103.97208404541016, -103.972, -103.0, -100.0, -90.0,
                      -87.3365447505531):
            values.extend(neighbors(value))
        # Sample the entire subnormal-output interval, without random/exhaustive CI loops.
        values.extend(fp32(-104.0 + i / 16) for i in range(267))
        for value in values:
            expected = fp32(decimal_golden('exp', value))
            actual = self.lib.cm_exp(value)
            with self.subTest(x=value):
                self.assertTrue(math.isfinite(actual) and actual >= 0)
                self.assertLessEqual(ulps(actual, expected), 1)
                if expected == 0:
                    self.assertEqual(bits(actual), bits(0.0))
                elif expected <= from_bits(0x007fffff):
                    self.assertGreater(actual, 0)

    def test_exp_normal_and_overflow_boundaries(self):
        values = [fp32(i / 4) for i in range(-349, 355)]
        for value in (-87.3, -87.0, 0.0, 88.0, 88.5, 88.72, 88.72283905206835,
                      89.0):
            values.extend(neighbors(value))
        values.append(from_bits(0x7f7fffff))
        for value in values:
            # Values beyond 100 overflow binary32, avoiding an enormous Decimal exponent.
            golden = math.inf if value > 100 else decimal_golden('exp', value)
            expected = fp32(golden)
            actual = self.lib.cm_exp(value)
            with self.subTest(x=value):
                if math.isinf(expected):
                    self.assertEqual(actual, math.inf)
                else:
                    self.assertTrue(math.isfinite(actual) and actual > 0)
                    self.assertLess(abs(actual / golden - 1), 8e-7)
                    self.assertLessEqual(ulps(actual, expected), 8)

    def test_log_special_values(self):
        for value in (0.0, -0.0):
            with self.subTest(x=value):
                self.assertEqual(self.lib.cm_log(value), -math.inf)
        for value in (-from_bits(1), -1.0, -from_bits(0x7f7fffff), -math.inf,
                      math.nan):
            with self.subTest(x=value):
                self.assertTrue(math.isnan(self.lib.cm_log(value)))
        self.assertEqual(self.lib.cm_log(math.inf), math.inf)
        self.assertEqual(bits(self.lib.cm_log(1.0)), bits(0.0))

    def test_log_subnormals_exponents_and_near_one(self):
        raw_values = {
            1, 2, 3, 0x003fffff, 0x00400000, 0x007ffffe, 0x007fffff,
            0x00800000, 0x00800001, 0x7f7ffffe, 0x7f7fffff
        }
        # All exponent bins, varied mantissas, and adjacent floats at powers of two.
        for exponent in range(1, 255):
            raw = exponent << 23
            raw_values.update(
                (raw - 1, raw, raw + 1, raw | 0x123456, raw | 0x654321)
            )
        raw_values.update(0x3f800000 + delta for delta in range(-32, 33))
        for raw in sorted(raw_values):
            value = from_bits(raw)
            expected = fp32(decimal_golden('ln', value))
            actual = self.lib.cm_log(value)
            with self.subTest(x=value, bits=hex(raw)):
                self.assertTrue(math.isfinite(actual))
                self.assertLessEqual(ulps(actual, expected), 3)

    def test_log_existing_absolute_budget(self):
        for i in range(1, 1001):
            value = fp32(i * .01)
            golden = decimal_golden('ln', value)
            with self.subTest(x=value):
                self.assertLess(abs(self.lib.cm_log(value) - golden), 7e-7)

    def test_sincos_signed_zero(self):
        for value in (0.0, -0.0):
            sn, cs = self.sincos(value)
            with self.subTest(x=value):
                self.assertEqual(bits(sn), bits(value))
                self.assertEqual(bits(cs), bits(1.0))

    def test_sincos_rejects_outside_supported_domain(self):
        outside = from_bits(bits(4096.0) + 1)
        for value in (outside, -outside, 4097.0, -4097.0,
                      from_bits(0x7f7fffff), -from_bits(0x7f7fffff), math.inf,
                      -math.inf, math.nan):
            sn, cs = self.sincos(value)
            with self.subTest(x=value):
                self.assertTrue(math.isnan(sn) and math.isnan(cs))

    def test_sincos_dense_and_integer_domain(self):
        # 32,769 exact quarter steps contain every integer in [-4096,4096].
        worst = (0.0, None)
        for i in range(-16384, 16385):
            value = i / 4
            actual = self.sincos(value)
            error = max(
                abs(actual[0] - math.sin(value)),
                abs(actual[1] - math.cos(value))
            )
            if not all(map(math.isfinite, actual)):
                error = math.inf
            if error > worst[0]:
                worst = (error, value)
        self.assertLess(
            worst[0], 1e-6, f'worst absolute error at x={worst[1]}'
        )

    def test_sincos_quadrant_float_neighbors(self):
        worst = (0.0, None)
        for quadrant in range(-2607, 2608):
            for value in neighbors(quadrant * math.pi / 2):
                actual = self.sincos(value)
                error = max(
                    abs(actual[0] - math.sin(value)),
                    abs(actual[1] - math.cos(value))
                )
                if not all(map(math.isfinite, actual)):
                    error = math.inf
                if error > worst[0]:
                    worst = (error, value)
        self.assertLess(
            worst[0], 1e-6, f'worst absolute error at x={worst[1]}'
        )

    def test_sincos_decimal_goldens(self):
        values = {
            -4096.0, 4096.0, -2047.0, 2047.0, -from_bits(1),
            from_bits(1)
        }
        for quadrant in (0, 1, 2, 3, 7, 16, 63, 128, 511, 1024, 2047, 2607):
            for sign in (-1, 1):
                values.update(neighbors(sign * quadrant * math.pi / 2))
        for value in sorted(values):
            golden = decimal_sincos(value)
            actual = self.sincos(value)
            with self.subTest(x=value):
                # Validate the independent FP64 sweep oracle to 2e-15 absolute.
                self.assertLess(abs(math.sin(value) - golden[0]), 2e-15)
                self.assertLess(abs(math.cos(value) - golden[1]), 2e-15)
                self.assertLess(abs(actual[0] - golden[0]), 1e-6)
                self.assertLess(abs(actual[1] - golden[1]), 1e-6)


if __name__ == '__main__':
    unittest.main()
