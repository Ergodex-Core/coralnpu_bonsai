"""Independent CPU oracle compared to separate FP64 scalar fixture equations."""
import json
from pathlib import Path
import tempfile
import unittest
try:
    import numpy as np
    from cpu_reference import Reference
except ImportError:
    np = None
from pack_model import sha256


@unittest.skipIf(np is None, 'NumPy is an optional CPU-reference dependency')
class ReferenceTests(unittest.TestCase):

    def test_three_formats_decode_and_repeatability(self):
        from runtime.test_runtime import fixture, reference
        for encoding, yarn, tied in [(1, False, False), (2, False, True),
                                     (3, True, True)]:
            image, cfg, weights = fixture(encoding, yarn, tied)
            with self.subTest(encoding=encoding
                              ), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / 'model.bin').write_bytes(image)
                (root / 'manifest.json').write_text(
                    json.dumps({
                        'model_id':
                        'fixture',
                        'segments': [{
                            'file': 'model.bin',
                            'bytes': len(image),
                            'sha256': sha256(root / 'model.bin')
                        }]
                    })
                )
                gold = reference(cfg, weights, [1, 2, 3])
                r = Reference(root / 'manifest.json')
                for pos, token in enumerate([1, 2, 3]):
                    actual = r.step(token)
                    expected = gold[10, cfg['n_layers'], pos]
                    np.testing.assert_allclose(
                        actual, expected, rtol=3e-5, atol=3e-5
                    )
                    self.assertEqual(
                        int(np.argmax(actual)), int(np.argmax(expected))
                    )
                first = Reference(root / 'manifest.json').generate([1], 3, [])
                self.assertEqual(
                    first,
                    Reference(root / 'manifest.json').generate([1], 3, [])
                )
                self.assertEqual(first[2], 'length')
                stopped = Reference(root / 'manifest.json'
                                    ).generate([1], 3, [first[0][0]])
                self.assertEqual(stopped[0], [first[0][0]])
                self.assertEqual(stopped[2], 'eos')


if __name__ == '__main__': unittest.main()
