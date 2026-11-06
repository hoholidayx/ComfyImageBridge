"""Provider-safe schema numbers do not narrow the runtime seed contract."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('model_schema_tests', ROOT / '__init__.py',
                                            submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)
from model_schema_tests.jobs import normalize


class ModelSchemaTest(unittest.TestCase):
    def test_tool_declarations_fit_signed_64_bit_json_decoders(self):
        def signed_integer(text):
            value = int(text)
            struct.pack('>q', value)
            return value

        declarations = [{'type': 'function', 'function': schema}
                        for schema in package.SCHEMAS.values()]
        decoded = json.loads(json.dumps(declarations), parse_int=signed_integer)
        self.assertEqual(decoded, declarations)

    def test_runtime_preserves_uint64_seed_boundaries(self):
        for seed, count in ((0, 1), (2**63, 1), (2**64 - 1, 1), (2**64 - 4, 4)):
            request = {'prompt': 'synthetic geometry', 'seed': seed, 'count': count}
            with self.subTest(seed=seed, count=count):
                self.assertEqual(normalize(request)['seed'], seed)
                package.validate_generation(request)
        for seed, count in ((-1, 1), (2**64, 1), (2**64 - 1, 2), (True, 1), (1.5, 1)):
            request = {'prompt': 'synthetic geometry', 'seed': seed, 'count': count}
            with self.subTest(seed=seed, count=count):
                for validate in (normalize, package.validate_generation):
                    with self.assertRaises(ValueError):
                        validate(request)


if __name__ == '__main__':
    unittest.main()
