"""A proven cached emission avoids only the unused current Bazel executable."""
import unittest
from unittest.mock import patch

import build


class CachedBazelTest(unittest.TestCase):

    def test_fresh_mode_requires_exact_installed_bazel(self):
        with patch.object(build, 'capture',
                          return_value='bazel 9.1.0') as capture:
            result = build.check_bazel()
            self.assertTrue(result['executed'])
            self.assertEqual(result['version'], '9.1.0')
            capture.assert_called_once_with(['bazel', '--version'])
        with patch.object(build, 'capture', return_value='bazel 8.6.0'):
            with self.assertRaises(RuntimeError):
                build.check_bazel()

    def test_cached_mode_records_historical_pin_without_executing_bazel(self):
        receipt = {
            'rtl_generation': 'cached_pinned_output_verified_not_regenerated',
            'artifact_emission_sha': build.PINS['coral_reference_commit'],
            'manifest_sha256': 'a' * 64
        }
        with patch.object(build, 'capture',
                          side_effect=AssertionError('Bazel executed')):
            result = build.check_bazel(receipt)
        self.assertFalse(result['executed'])
        self.assertIsNone(result['version'])
        self.assertEqual(result['historical_generator_version_pin'], '9.1.0')

    def test_unvalidated_cache_receipt_does_not_omit_bazel_check(self):
        for receipt in ({}, {'rtl_generation': 'not_generated'},
                        {'rtl_generation':
                         'cached_pinned_output_verified_not_regenerated',
                         'artifact_emission_sha': 'a' * 40, 'manifest_sha256':
                         'b' * 64}):
            with self.subTest(receipt=receipt
                              ), self.assertRaises(RuntimeError):
                build.check_bazel(receipt)


if __name__ == '__main__':
    unittest.main()
