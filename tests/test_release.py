import hashlib,io,json,os,pathlib,struct,subprocess,sys,tarfile,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import configure_release,download_resources,package_release,resolve_inputs,validate_artifacts

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.entry=resolve_inputs.resolve('v2.3.2')
        self.entry=json.loads(json.dumps(self.entry))
        self.entry['installer_original_version']='v2.3.2'
        for rel in self.entry['installer_sha256']:
            content=(f'ORIGINAL_VERSION=v2.3.2\n' if rel=='1pctl' else f'# fixture {rel}\n').encode()
            file=self.root/rel;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(content);file.chmod(0o755)
            self.entry['installer_sha256'][rel]=hashlib.sha256(content).hexdigest()
        (self.root/'GeoIP.mmdb').write_bytes(b'fixture geo')
        self.entry['geoip_sha256']=hashlib.sha256(b'fixture geo').hexdigest()
        for part in ('core','agent'):
            (self.root/f'1panel-{part}.service').write_bytes((self.root/f'initscript/1panel-{part}.service').read_bytes())
        (self.root/'build').mkdir()
        for module in (package_release,validate_artifacts,download_resources):
            p=patch.object(module,'resolve',return_value=self.entry);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'BUILD_REPOSITORY_COMMIT':'a'*40},clear=False);p.start();self.addCleanup(p.stop)
    def binaries(self,arch):
        cls,end,machine=validate_artifacts.ARCHES[arch]
        data=bytearray(64);data[:6]=b'\x7fELF'+bytes((cls,end));data[18:20]=machine.to_bytes(2,'little' if end==1 else 'big')
        for part in ('core','agent'):
            p=self.root/'build'/f'1panel-{part}';p.write_bytes(data);p.chmod(0o755)
    def build(self,arches):
        for arch in arches:self.binaries(arch);package_release.package(self.root,'v2.3.2',arch)
        package_release.finalize(self.root,'v2.3.2',arches)
    def test_seven_architecture_matrix(self):
        arches=list(validate_artifacts.ARCHES);self.build(arches)
        self.assertEqual(len(validate_artifacts.validate_dist(self.root/'dist','v2.3.2',arches)),7)
    def test_export_preserves_destination_directory(self):
        source=self.root/'export-source';source.mkdir();(source/'dist').mkdir()
        (source/'dist/artifact').write_bytes(b'verified')
        destination=self.root/'export-destination';destination.mkdir();destination.chmod(0o750)
        before=destination.stat()
        dockerfile=(pathlib.Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
        command=json.loads(next(line[4:] for line in dockerfile.splitlines() if line.startswith('CMD ')))
        command[-1]=command[-1].replace('/dist/',str(destination)+'/')
        subprocess.run(command,cwd=source,check=True)
        after=destination.stat()
        self.assertEqual((before.st_uid,before.st_gid,before.st_mode),(after.st_uid,after.st_gid,after.st_mode))
        (destination/'build-inputs.env').write_text('SOURCE_COMMIT=fixture\n')
        self.assertEqual((destination/'artifact').read_bytes(),b'verified')
    def test_download_success_replaces_zero_files(self):
        destination=self.root/'new-resources';destination.mkdir();(destination/'install.sh').write_bytes(b'')
        def fake(url,path,digest):
            rel='GeoIP.mmdb' if url==self.entry['geoip_url'] else url.split(self.entry['installer_commit']+'/',1)[1]
            path.write_bytes((self.root/rel).read_bytes())
        with patch.object(download_resources,'download',side_effect=fake):
            download_resources.main('v2.3.2',destination)
        self.assertEqual((destination/'install.sh').read_bytes(),(self.root/'install.sh').read_bytes())
        self.assertTrue((destination/'resource-provenance.json').is_file())
    def test_download_failure_after_partial_stage_preserves_all(self):
        before={p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        count=0
        def fake(url,path,digest):
            nonlocal count
            count+=1
            if count==3:raise ValueError('failed halfway')
            path.write_bytes(b'downloaded')
        with patch.object(download_resources,'download',side_effect=fake):
            with self.assertRaises(ValueError):download_resources.main('v2.3.2',self.root)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})
    def test_truncated_gzip_rejected(self):
        self.build(['amd64']);p=next((self.root/'dist').glob('*.tar.gz'));p.write_bytes(p.read_bytes()[:-6])
        with self.assertRaises((EOFError,OSError)):
            validate_artifacts.validate_package(p,'v2.3.2','amd64')
    def test_1pctl_changes_rejected(self):
        p=self.root/'1pctl';p.write_text(p.read_text()+'# tampered\n');self.binaries('amd64')
        package_release.package(self.root,'v2.3.2','amd64')
        with self.assertRaisesRegex(ValueError,'1pctl content mismatch'):package_release.finalize(self.root,'v2.3.2',['amd64'])
    def test_wrong_elf_target(self):
        self.binaries('s390x')
        with self.assertRaisesRegex(ValueError,'Wrong ELF'):package_release.package(self.root,'v2.3.2','amd64')
    def test_corrupt_checksum(self):
        self.build(['amd64']);f=next((self.root/'dist').glob('*.sha256'));f.write_text('bad')
        with self.assertRaisesRegex(ValueError,'Checksum'):validate_artifacts.validate_dist(self.root/'dist','v2.3.2',['amd64'])
    def test_absolute_checksum_rejected(self):
        self.build(['amd64']);f=next((self.root/'dist').glob('*.sha256'));f.write_text(f.read_text().replace('  1panel','  /opt/1Panel/1panel'))
        with self.assertRaises(ValueError):validate_artifacts.validate_dist(self.root/'dist','v2.3.2',['amd64'])
    def test_missing_architecture(self):
        self.build(['amd64'])
        with self.assertRaises(FileNotFoundError):validate_artifacts.validate_dist(self.root/'dist','v2.3.2',['amd64','arm64'])
    def test_empty_required_file(self):
        (self.root/'lang/en.sh').write_bytes(b'');self.binaries('amd64');package_release.package(self.root,'v2.3.2','amd64')
        with self.assertRaisesRegex(ValueError,'Empty file'):package_release.finalize(self.root,'v2.3.2',['amd64'])
    def test_download_failure_keeps_existing_file(self):
        before=(self.root/'install.sh').read_bytes()
        with patch.object(download_resources,'download',side_effect=ValueError('network failed')):
            with self.assertRaises(ValueError):download_resources.main('v2.3.2',self.root)
        self.assertEqual(before,(self.root/'install.sh').read_bytes())
        self.assertFalse(list(self.root.glob('.resources-*')))
    def test_empty_download_rejected(self):
        p=self.root/'empty'
        def fake(args,check):p.write_bytes(b'')
        with patch.object(download_resources.subprocess,'run',side_effect=fake):
            with self.assertRaisesRegex(ValueError,'Empty download'):download_resources.download('https://example.test/x',p,'0'*64)
    def test_wrong_download_digest_rejected(self):
        p=self.root/'wrong'
        def fake(args,check):p.write_bytes(b'bad')
        with patch.object(download_resources.subprocess,'run',side_effect=fake):
            with self.assertRaisesRegex(ValueError,'Checksum mismatch'):download_resources.download('https://example.test/x',p,'0'*64)
    def test_unknown_historical_version_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'no reviewed input lock'):resolve_inputs.resolve('v2.0.13')
    def test_invalid_architectures(self):
        for text in ('','amd64 amd64','amd64 bogus','$(bad)'):
            with self.assertRaises(ValueError):validate_artifacts.architectures(text)
    def test_production_community_config(self):
        # Exact reviewed source layouts at 65243c68c463cc055ab044093f641ea5d2e9e28b.
        # In particular Agent has no version key, unlike Core.
        fixtures={'core': 'base:\n  install_dir: /opt\n  mode: dev\n  is_demo: false\n  is_offline: false\n  is_fxplay: false\n  is_enterprise: false\n  port: 9999\n  username: admin\n  password: admin123\n  version: v2.0.0\n\nlog:\n  level: debug\n  time_zone: Asia/Shanghai\n  log_name: 1Panel-Core\n  log_suffix: .log\n  max_backup: 10\n', 'agent': 'base:\n  install_dir: /opt\n  mode: dev\n  is_demo: false\n  is_offline: false\n  is_fxplay: false\n  is_enterprise: false\n\nlog:\n  level: debug\n  time_zone: Asia/Shanghai\n  log_name: 1Panel\n  log_suffix: .log\n  max_backup: 10\n'}
        for part,fixture in fixtures.items():
            p=self.root/part/'cmd/server/conf/app.yaml';p.parent.mkdir(parents=True);p.write_text(fixture)
        configure_release.configure(self.root,'v2.3.2')
        for part,fixture in fixtures.items():
            content=(self.root/part/'cmd/server/conf/app.yaml').read_text()
            expected=fixture.replace('  mode: dev\n','  mode: stable\n').replace('  level: debug\n','  level: info\n')
            if part=='core': expected=expected.replace('  version: v2.0.0\n','  version: v2.3.2\n')
            self.assertEqual(content,expected)  # Every other line is preserved.
            self.assertIn('  is_enterprise: false\n',content)
            if part=='agent': self.assertNotIn('version:',content)
    def test_missing_core_version_fails_closed(self):
        for part in ('core','agent'):
            p=self.root/part/'cmd/server/conf/app.yaml';p.parent.mkdir(parents=True)
            p.write_text('base:\n  mode: dev\n  is_demo: false\n  is_offline: false\n  is_fxplay: false\n  is_enterprise: false\nlog:\n  level: debug\n')
        with self.assertRaisesRegex(ValueError,'expected exactly one version, found 0'):
            configure_release.configure(self.root,'v2.3.2')
    def test_stale_files_rejected(self):
        self.build(['amd64']);(self.root/'dist/stale').write_text('stale')
        with self.assertRaisesRegex(ValueError,'Unexpected'):validate_artifacts.validate_dist(self.root/'dist','v2.3.2',['amd64'])
if __name__=='__main__':unittest.main()
