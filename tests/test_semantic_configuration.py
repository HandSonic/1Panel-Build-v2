import copy
import hashlib
import json
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

class SemanticConfigurationTests(unittest.TestCase):
    def test_all_reviewed_fixtures_and_benign_variations(self):
        registry=json.loads((ROOT/'config/embedded-configs.json').read_text())
        for version,entry in registry.items():
            if any(p.get('source_acquisition') for p in entry['components'].values()):continue  # Synthetic HTTPS tests cover this route.
            for component in ['core','agent']:
                old,new,_=expected_bytes(version,component)
                parsed=yaml.safe_load(old)
                variants=[old,old.rstrip(b'\n'),old.rstrip(b'\n')+b'\n',old.replace(b'\n',b'\r\n'),
                          b'# benign comment\n'+old,
                          yaml.safe_dump(parsed,sort_keys=True,indent=4).encode(),
                          yaml.safe_dump(parsed,sort_keys=False,default_flow_style=True).encode()]
                for index,variant in enumerate(variants):
                    with self.subTest(version=version,component=component,variation=index):
                        normalized=production(variant,version,component,entry['mode'])
                        self.assertEqual(semantic(normalized),semantic(new))
                        self.assertEqual(production(normalized,version,component,entry['mode']),normalized)
                # New fields are preserved; optional legacy fields are not invented.
                added=copy.deepcopy(parsed);added['base']['future_option']={'flag':True,'items':[1,'two']}
                normalized=yaml.safe_load(production(yaml.safe_dump(added),version,component,entry['mode']))
                self.assertEqual(normalized['base']['future_option'],added['base']['future_option'])
                for flag in ['is_demo','is_offline','is_fxplay','is_enterprise']:
                    added['base'].pop(flag,None)
                normalized=yaml.safe_load(production(yaml.safe_dump(added),version,component,entry['mode']))
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

    def test_configure_accepts_formatting_but_emits_reviewed_bytes(self):
        for version,entry in json.loads((ROOT/'config/embedded-configs.json').read_text()).items():
            if any(p.get('source_acquisition') for p in entry['components'].values()):continue  # Synthetic HTTPS tests cover this route.
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                for component in ['core','agent']:
                    old,new,commit=expected_bytes(version,component)
                    path=root/component/'cmd/server/conf/app.yaml';path.parent.mkdir(parents=True)
                    path.write_text('# same source semantics\n'+yaml.safe_dump(yaml.safe_load(old),sort_keys=True,indent=4))
                with patch.object(configure_release,'resolve',return_value={'source_commit':commit,'mode':'stable'}):
                    configure_release.configure(root,version)
                for component in ['core','agent']:
                    self.assertEqual((root/component/'cmd/server/conf/app.yaml').read_bytes(),expected_bytes(version,component)[1])

    def test_configure_rejects_unreviewed_semantic_change_without_partial_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);before={}
            for component in ['core','agent']:
                old,_,commit=expected_bytes('v2.3.2',component)
                if component=='agent':old+=b'new_unknown_section: changed\n'
                path=root/component/'cmd/server/conf/app.yaml';path.parent.mkdir(parents=True);path.write_bytes(old);before[component]=old
            with patch.object(configure_release,'resolve',return_value={'source_commit':commit,'mode':'stable'}):
                with self.assertRaises(ValueError):configure_release.configure(root,'v2.3.2')
            for component in ['core','agent']:
                self.assertEqual((root/component/'cmd/server/conf/app.yaml').read_bytes(),before[component])

    def test_readiness_covers_all_versions_without_enabling_incomplete_locks(self):
        readiness=json.loads((ROOT/'config/historical-input-readiness.json').read_text())
        locks=json.loads((ROOT/'config/sources.json').read_text())
        self.assertEqual(len(readiness['versions']),40)
        self.assertEqual(set(readiness['enabled_versions']),set(locks))
        for row in readiness['versions']:
            self.assertEqual(row['source_lock_enabled'],row['version'] in locks)
            if row['version'] not in locks:
                self.assertIn('frontend_package_lock_json',row['missing_review_inputs'])
                self.assertIn('unpublished_full_seven_architecture_build',row['required_validation'])

    def test_runtime_dependency_is_pinned_and_isolated(self):
        workflow=(ROOT/'.github/workflows/build.yml').read_text()
        self.assertIn('python3 -m venv "$RUNNER_TEMP/build-python"',workflow)
        self.assertIn('PyYAML==6.0.3',workflow)
        self.assertIn('ENV PATH="/opt/build-python/bin:$PATH"',(ROOT/'Dockerfile').read_text())
        self.assertIn('python3 -m venv /tmp/build-python',(ROOT/'.cnb.yml').read_text())
        self.assertIn('PyYAML==6.0.3',(ROOT/'.cnb.yml').read_text())
