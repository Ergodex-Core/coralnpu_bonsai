"""The strict operator schema contains every mandatory runtime setting."""
import ast
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from validate_bundle import activation_errors


class ConfigurationSchemaTests(unittest.TestCase):

    def test_runtime_required_keys_are_in_operator_schema(self):
        fields = set(json.loads((ROOT / 'config.example.json').read_text()))
        for path in (ROOT / 'host').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Subscript)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)):
                    continue
                value = node.value
                is_config = (
                    isinstance(value, ast.Name) and value.id == 'config'
                )
                is_config |= (
                    isinstance(value, ast.Attribute) and value.attr == 'config'
                )
                if is_config:
                    with self.subTest(path=path.name, line=node.lineno):
                        self.assertIn(node.slice.value, fields)

    def test_network_proof_path_is_fixed(self):
        config = json.loads((ROOT / 'config.example.json').read_text())
        message = 'staging network proof path differs from fixed reviewed path'
        self.assertNotIn(message, activation_errors(config))
        for path in ('/tmp/untrusted.json', None):
            config['staging_network_qualification_path'] = path
            self.assertIn(message, activation_errors(config))


if __name__ == '__main__':
    unittest.main()
