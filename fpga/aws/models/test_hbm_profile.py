import unittest
from hbm_profile import *


class HBMTests(unittest.TestCase):

    def test_all_bank_edges_and_translation(self):
        for bank in range(8):
            self.assertEqual(
                transfer_offset(bank, 0x80000000, 1), bank * 0x80000000
            )
            self.assertEqual(
                transfer_offset(bank, 0xffffffff, 1),
                (bank + 1) * 0x80000000 - 1
            )
            self.assertEqual(
                transfer_offset(bank, 0x80000000, 0x80000000),
                bank * 0x80000000
            )
        self.assertEqual(transfer_offset(7, 0xfffffffc, 4), 0x3fffffffc)

    def test_no_wrap_or_cross_bank(self):
        for args in [(0, 0xffffffff, 2), (0, 0x100000000, 1),
                     (-1, 0x80000000, 4), (8, 0x80000000, 4),
                     (0, 0x7fffffff, 4), (0, 0x80000000, 0),
                     (0, 0x80000000, -1), (0, 0x80000000, 1 << 64)]:
            with self.assertRaises(ValueError):
                transfer_offset(*args)

    def test_host_dma_not_covered_by_npu_counts(self):
        with self.assertRaises(ValueError):
            bank_change_guard(
                SIGNATURE, 7, 0, reset_asserted=False, host_drained=False
            )
        self.assertTrue(
            bank_change_guard(
                SIGNATURE, 7, 0, reset_asserted=False, host_drained=True
            )
        )
        self.assertTrue(
            bank_change_guard(
                SIGNATURE, 5, 0, reset_asserted=True, host_drained=True
            )
        )

    def test_guard_rejects_bad_state(self):
        for sig, status, counts, reset in [
            (0, 7, 0, True), (SIGNATURE, 6, 0, True), (SIGNATURE, 5, 0, False),
            (SIGNATURE, 3, 0, True), (SIGNATURE, 7, 1, True),
            (SIGNATURE, 7, 0x100, True)
        ]:
            with self.assertRaises(ValueError):
                bank_change_guard(
                    sig,
                    status,
                    counts,
                    reset_asserted=reset,
                    host_drained=True
                )


if __name__ == '__main__': unittest.main()
