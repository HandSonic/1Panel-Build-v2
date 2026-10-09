import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from semantic_configuration import production, semantic
from embedded_configuration import expected_bytes
import configure_release
from test_discovered_inputs import fixture, synthetic_runtime, configuration_shapes

class SemanticConfigurationTests(unittest.TestCase):
    def test_synthetic_source_shapes_and_benign_variations(self):
        for shape,vendor in configuration_shapes():
            value=fixture(vendor=vendor);version=value['version']
            with tempfile.TemporaryDirectory() as tmp,synthetic_runtime(Path(tmp),value,vendor):
                for component in ['core','agent']:
                    old,new,_=expected_bytes(version,component)
                    parsed=yaml.safe_load(old)
                    variants=[old,old.rstrip(b'\n'),old.rstrip(b'\n')+b'\n',old.replace(b'\n',b'\r\n'),
                              b'# benign comment\n'+old,
                              yaml.safe_dump(parsed,sort_keys=True,indent=4).encode(),
                              yaml.safe_dump(parsed,sort_keys=False,default_flow_style=True).encode()]
                    for index,variant in enumerate(variants):
                        with self.subTest(shape=shape,component=component,variation=index):
                            normalized=production(variant,version,component,value['mode'])
                            self.assertEqual(semantic(normalized),semantic(new))
                            self.assertEqual(production(normalized,version,component,value['mode']),normalized)
                    added=copy.deepcopy(parsed);added['base']['future_option']={'flag':True,'items':[1,'two']}
                    normalized=yaml.safe_load(production(yaml.safe_dump(added),version,component,value['mode']))
                    self.assertEqual(normalized['base']['future_option'],added['base']['future_option'])
                    for flag in ['is_demo','is_offline','is_fxplay','is_enterprise']:added['base'].pop(flag,None)
                    normalized=yaml.safe_load(production(yaml.safe_dump(added),version,component,value['mode']))
                    self.assertFalse(set(['is_demo','is_offline','is_fxplay','is_enterprise']) & normalized['base'].keys())
                    self.assertEqual(old.endswith(b'\n'),new.endswith(b'\n'))

    def test_typed_semantic_integrity(self):
        self.assertNotEqual(semantic('a: false'),semantic('a: 0'))
        self.assertNotEqual(semantic('a: 1'),semantic('a: 1.0'))
        self.assertEqual(semantic('a: true # note\nb: [1, 2]'),semantic('b:\n - 1\n - 2\na: true'))

    def test_rejects_ambiguous_or_wrong_critical_values(self):
        good='base:\n  mode: dev\n  version: v2.0.0\n  is_demo: false\nlog:\n  level: debug\n'
        invalid=[good.replace('  mode: dev','  mode: dev\n  mode: stable'),
                 good.replace('  mode: dev','  mode: 1'),good.replace('  version: v2.0.0\n',''),
                 good.replace('  level: debug','  level: null'),
                 good.replace('  is_demo: false','  is_demo: "false"'),
                 good.replace('  is_demo: false','  is_demo: 0'),
                 good+'other: &thing [a]\ncopy: *thing\n',good+'other: !!str x\n',
                 good+'other: {x: 1, x: 2}\n',good+'---\nx: y\n',
                 good.replace('  mode: dev','  mode: |\n    dev'),
                 good+'? [a, b]\n: c\n','[not, a, mapping]',b'\xff']
        for text in invalid:
            with self.subTest(text=text),self.assertRaises(ValueError):
                production(text,'v2.3.2','core','stable')

    def test_normalizes_only_intended_sections(self):
        source='base:\n  mode: dev\n  version: old\n  future: keep\nlog:\n  level: debug\nother:\n  mode: special\n  level: warn\n  version: private\n'
        result=production(source,'v2.3.2','core','stable').decode()
        self.assertIn('other:\n  mode: special\n  level: warn\n  version: private',result)
        self.assertIn('future: keep',result)

    def test_configure_accepts_formatting_but_emits_contract_bytes(self):
        for shape,vendor in configuration_shapes():
            value=fixture(vendor=vendor);version=value['version']
            with self.subTest(shape=shape),tempfile.TemporaryDirectory() as tmp,synthetic_runtime(Path(tmp),value,vendor):
                root=Path(tmp)
                for component in ['core','agent']:
                    old,new,commit=expected_bytes(version,component)
                    path=root/component/'cmd/server/conf/app.yaml';path.parent.mkdir(parents=True)
                    path.write_text('# same source semantics\n'+yaml.safe_dump(yaml.safe_load(old),sort_keys=True,indent=4))
                configure_release.configure(root,version)
                for component in ['core','agent']:
                    self.assertEqual((root/component/'cmd/server/conf/app.yaml').read_bytes(),expected_bytes(version,component)[1])
                # Repeated configuration remains byte-identical.
                configure_release.configure(root,version)
                for component in ['core','agent']:
                    self.assertEqual((root/component/'cmd/server/conf/app.yaml').read_bytes(),expected_bytes(version,component)[1])

    def test_configure_rejects_semantic_or_source_change_without_partial_write(self):
        for mutation in ('semantic','source-commit'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp,synthetic_runtime(Path(tmp)):
                root=Path(tmp);before={}
                for component in ['core','agent']:
                    old,_,commit=expected_bytes('v2.99.0',component)
                    if component=='agent' and mutation=='semantic':old+=b'new_unknown_section: changed\n'
                    path=root/component/'cmd/server/conf/app.yaml';path.parent.mkdir(parents=True);path.write_bytes(old);before[component]=old
                if mutation=='source-commit':
                    with patch.object(configure_release,'resolve',return_value={'source_commit':'0'*40,'mode':'stable'}),self.assertRaises(ValueError):
                        configure_release.configure(root,'v2.99.0')
                else:
                    with self.assertRaises(ValueError):configure_release.configure(root,'v2.99.0')
                for component in ['core','agent']:
                    self.assertEqual((root/component/'cmd/server/conf/app.yaml').read_bytes(),before[component])

    def test_exact_component_contract_hashes_are_source_bound(self):
        for shape,vendor in configuration_shapes():
            value=fixture(vendor=vendor)
            with self.subTest(shape=shape),tempfile.TemporaryDirectory() as tmp,synthetic_runtime(Path(tmp),value,vendor):
                self.assertEqual(set(value['configuration']),{'core','agent'})
                for component in ('core','agent'):
                    original,normalized,commit=expected_bytes(value['version'],component)
                    profile=value['configuration'][component]
                    self.assertEqual(commit,value['source']['commit'])
                    self.assertEqual(hashlib.sha256(original).hexdigest(),profile['source_sha256'])
                    self.assertEqual(hashlib.sha256(normalized).hexdigest(),profile['normalized_sha256'])
                    self.assertEqual(value['source']['files'][profile['path']],{'sha256':profile['source_sha256'],'bytes':len(original)})

    def test_runtime_dependency_is_pinned_and_isolated(self):
        workflow=(ROOT/'.github/workflows/build.yml').read_text()
        self.assertIn('python3 -m venv "$RUNNER_TEMP/build-python"',workflow)
        self.assertIn('PyYAML==6.0.3',workflow)
        self.assertIn('ENV PATH="/opt/build-python/bin:$PATH"',(ROOT/'Dockerfile').read_text())
        self.assertIn('python3 -m venv /tmp/build-python',(ROOT/'.cnb.yml').read_text())
        self.assertIn('PyYAML==6.0.3',(ROOT/'.cnb.yml').read_text())
