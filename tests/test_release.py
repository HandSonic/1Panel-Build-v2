import hashlib,io,json,os,pathlib,struct,subprocess,sys,tarfile,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
import configure_release,download_resources,package_release,resolve_inputs,validate_artifacts
from embedded_configuration import expected_bytes

def executable_elf(arch,payload=b'\0'):
    """A structural ELF fixture with one file-backed executable load segment."""
    cls,end,machine=validate_artifacts.ARCHES[arch]
    order='<' if end==1 else '>'
    header_size,program_size=(52,32) if cls==1 else (64,56)
    size=header_size+program_size+len(payload);base=0x400000
    ident=b'\x7fELF'+bytes((cls,end,1))+b'\0'*9
    header=struct.pack(order+('HHIIIIIHHHHHH' if cls==1 else 'HHIQQQIHHHHHH'),
        2,machine,1,base+header_size+program_size,header_size,0,0,header_size,program_size,1,0,0,0)
    program=struct.pack(order+('IIIIIIII' if cls==1 else 'IIQQQQQQ'),
        *((1,0,base,base,size,size,5,4096) if cls==1 else (1,5,0,base,base,size,size,4096)))
    return ident+header+program+payload


class ELFValidation(unittest.TestCase):
    def test_executable_headers_support_32_bit_and_big_endian_targets(self):
        for arch in ('amd64','armv7','s390x'):
            with self.subTest(arch=arch):validate_artifacts.validate_elf(executable_elf(arch),arch)
        pie=bytearray(executable_elf('amd64'));struct.pack_into('<H',pie,16,3)
        validate_artifacts.validate_elf(pie,'amd64')

    def test_nonexecutable_or_truncated_elf_is_rejected(self):
        original=executable_elf('amd64')
        cases={'old_header_only':original[:64],'truncated_program_table':original[:119]}
        for name,offset,fmt,value in [('relocatable',16,'H',1),('missing_programs',56,'H',0),
            ('programs_outside_file',32,'Q',len(original)),('segment_outside_file',72,'Q',1),
            ('nonexecutable_segment',68,'I',4),('entry_outside_segment',24,'Q',0x500000)]:
            data=bytearray(original);struct.pack_into('<'+fmt,data,offset,value);cases[name]=data
        for name,data in cases.items():
            with self.subTest(case=name),self.assertRaises(ValueError):validate_artifacts.validate_elf(data,'amd64')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        import embedded_configuration
        from resolved_contract import INSTALLER_REQUIRED
        from semantic_configuration import production
        self.entry={'source_commit':'c'*40,'installer_commit':'d'*40,'mode':'stable',
                    'go_version':'1.26.1','node_version':'22.22.1','npm_version':'10.9.4',
                    'installer_sha256':dict.fromkeys(INSTALLER_REQUIRED,''),
                    'geoip_url':'https://resource.fit2cloud.com/1panel/package/v2/geo/GeoIP.mmdb'}
        self.configs={part:('base:\n  mode: dev\n  is_enterprise: false\n'+('  version: v0.0.0\n' if part=='core' else '')+'log:\n  level: debug\n').encode() for part in ('core','agent')}
        def synthetic_expected(version,part,*args):
            raw=self.configs[part]
            return raw,production(raw,version,part,'stable'),'c'*40
        for module in (sys.modules[__name__],embedded_configuration,configure_release):
            handle=patch.object(module,'expected_bytes',side_effect=synthetic_expected);handle.start();self.addCleanup(handle.stop)
        handle=patch.object(configure_release,'resolve',return_value=self.entry);handle.start();self.addCleanup(handle.stop)
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
        for part in ('core','agent'):
            p=self.root/'build'/f'1panel-{part}';p.write_bytes(executable_elf(arch,expected_bytes('v2.3.2',part)[1]));p.chmod(0o755)
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
        self.build(['amd64']);p=next((self.root/'dist').glob('*.tar.gz'));raw=p.read_bytes()
        bad_crc=bytearray(raw);bad_crc[-8]^=1
        for name,body in [('truncated',raw[:-6]),('bad_crc',bad_crc)]:
            with self.subTest(case=name):
                p.write_bytes(body)
                with self.assertRaises((EOFError,OSError)):
                    validate_artifacts.validate_package(p,'v2.3.2','amd64')
    def test_unsafe_duplicate_or_linked_archive_members_are_rejected(self):
        self.build(['amd64']);path=next((self.root/'dist').glob('*.tar.gz'))
        with tarfile.open(path) as archive:
            original=[(member,archive.extractfile(member).read() if member.isfile() else None) for member in archive.getmembers()]
        prefix='1panel-v2.3.2-linux-amd64/'
        for name,kind in [(prefix+'../escape',tarfile.REGTYPE),(prefix+'1pctl',tarfile.REGTYPE),
                          (prefix+'unexpected-link',tarfile.SYMTYPE)]:
            with self.subTest(name=name):
                with tarfile.open(path,'w:gz') as archive:
                    for member,body in original:archive.addfile(member,io.BytesIO(body) if body is not None else None)
                    info=tarfile.TarInfo(name);info.type=kind;info.mode=0o755
                    if kind==tarfile.SYMTYPE:info.linkname='../../outside';body=None
                    else:body=b'x';info.size=len(body)
                    archive.addfile(info,io.BytesIO(body) if body is not None else None)
                with self.assertRaisesRegex(ValueError,'Unsafe|Duplicate|nonregular'):
                    validate_artifacts.validate_package(path,'v2.3.2','amd64')
    def test_unmarked_metadata_is_rejected_by_complete_package_validation(self):
        self.build(['amd64']);path=next((self.root/'dist').glob('*.tar.gz'))
        with tarfile.open(path) as archive:
            original=[(member,archive.extractfile(member).read() if member.isfile() else None) for member in archive.getmembers()]
        with tarfile.open(path,'w:gz') as archive:
            for member,body in original:archive.addfile(member,io.BytesIO(body) if body is not None else None)
            info=tarfile.TarInfo('1panel-v2.3.2-linux-amd64/build-metadata/unexpected.json')
            body=b'{}';info.size=len(body);archive.addfile(info,io.BytesIO(body))
        with self.assertRaisesRegex(ValueError,'Unmarked package metadata'):
            validate_artifacts.validate_package(path,'v2.3.2','amd64')
    def test_publication_requires_embedded_inputs_in_the_complete_validation(self):
        self.build(['amd64']);path=next((self.root/'dist').glob('*.tar.gz'))
        # Legacy imports may be read before migration, but cannot be published.
        validate_artifacts.validate_package(path,'v2.3.2','amd64')
        with self.assertRaisesRegex(ValueError,'requires embedded build inputs'):
            validate_artifacts.validate_dist(self.root/'dist','v2.3.2',['amd64'],require_embedded=True)
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
    def test_missing_or_different_run_contract_fails_closed(self):
        import resolved_contract
        from test_discovered_inputs import fixture
        runtime=resolved_contract.runtime_contract
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            with patch.object(resolved_contract,'runtime_contract',side_effect=lambda version:runtime(version,root)):
                with self.assertRaises(FileNotFoundError):resolve_inputs.resolve('v2.0.13')
                (root/'config').mkdir()
                (root/'config/resolved-source.json').write_bytes(resolved_contract.canonical(fixture()))
                with self.assertRaises(ValueError):resolve_inputs.resolve('v2.0.13')
    def test_invalid_architectures(self):
        for text in ('','amd64 amd64','amd64 bogus','$(bad)'):
            with self.assertRaises(ValueError):validate_artifacts.architectures(text)
    def test_production_community_config(self):
        fixtures={part:raw.decode() for part,raw in self.configs.items()}
        for part,fixture in fixtures.items():
            p=self.root/part/'cmd/server/conf/app.yaml';p.parent.mkdir(parents=True);p.write_text(fixture)
        configure_release.configure(self.root,'v2.3.2')
        for part,fixture in fixtures.items():
            content=(self.root/part/'cmd/server/conf/app.yaml').read_text()
            expected=fixture.replace('  mode: dev\n','  mode: stable\n').replace('  level: debug\n','  level: info\n')
            if part=='core': expected=expected.replace('  version: v0.0.0\n','  version: v2.3.2\n')
            self.assertEqual(content,expected)  # Every other line is preserved.
            self.assertIn('  is_enterprise: false\n',content)
            if part=='agent': self.assertNotIn('version:',content)
    def test_missing_core_version_fails_closed(self):
        for part in ('core','agent'):
            p=self.root/part/'cmd/server/conf/app.yaml';p.parent.mkdir(parents=True)
            p.write_text('base:\n  mode: dev\n  is_demo: false\n  is_offline: false\n  is_fxplay: false\n  is_enterprise: false\nlog:\n  level: debug\n')
        with self.assertRaisesRegex(ValueError,'critical setting|source YAML differs'):
            configure_release.configure(self.root,'v2.3.2')
    def test_embedded_dev_configuration_rejected_despite_valid_manifest(self):
        self.binaries('amd64')
        path=self.root/'build/1panel-core'
        path.write_bytes(executable_elf('amd64',expected_bytes('v2.3.2','core')[0]))
        package_release.package(self.root,'v2.3.2','amd64')
        with self.assertRaisesRegex(ValueError,'normalized production configuration'):
            package_release.finalize(self.root,'v2.3.2',['amd64'])
    def test_stale_files_rejected(self):
        self.build(['amd64']);(self.root/'dist/stale').write_text('stale')
        with self.assertRaisesRegex(ValueError,'Unexpected'):validate_artifacts.validate_dist(self.root/'dist','v2.3.2',['amd64'])
if __name__=='__main__':unittest.main()
