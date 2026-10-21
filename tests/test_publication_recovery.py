"""Recover verified CI packages using pinned IDs and GitHub's SHA metadata."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import manual_publication as manual
import matrix_contract as matrix
from release_asset_repair import GitHub
import test_matrix_contract as matrix_tests


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RemoteRelease(GitHub):
    """Use the real adapter/transaction above a small, stateful HTTP fixture."""
    def __init__(self,version,release,files,*,change_tag=False):
        super().__init__(manual.UPSTREAM,version)
        self.releases={release['id']:copy.deepcopy(release)}
        self.tag_id=release['id']
        self.change_tag=change_tag
        self.calls=[]
        self.uploads=[]
        self.downloads=[]
        self.contents={}
        for index,path in enumerate(files,100):
            self.contents[index]=path.read_bytes()
        if change_tag:
            decoy=copy.deepcopy(release);decoy['id']=18;decoy['assets']=[]
            self.releases[18]=decoy

    def run(self,*args):
        self.calls.append(args)
        if args[0]!='api':raise AssertionError(args)
        method=args[args.index('--method')+1] if '--method' in args else 'GET'
        endpoint=next(value for value in args[1:] if value.startswith(('repos/','https://')))
        parsed=urlparse(endpoint)
        path=parsed.path if parsed.scheme else endpoint.split('?',1)[0]
        prefix=f'repos/{self.repo}/releases'
        if method=='GET' and path==prefix+'/tags/'+self.tag:
            release=self.releases[self.tag_id]
            if release['draft']:
                raise subprocess.CalledProcessError(1,['gh',*args],stderr='gh: Not Found (HTTP 404)')
            result=copy.deepcopy(release)
            if self.change_tag:self.tag_id=18
            return json.dumps(result)
        if method=='GET' and path==prefix:
            return json.dumps(list(self.releases.values()))
        if method=='GET' and path.startswith(prefix+'/'):
            return json.dumps(self.releases[int(path.rsplit('/',1)[1])])
        if method=='POST' and parsed.hostname=='uploads.github.com':
            release_id=int(path.split('/')[-2]);release=self.releases[release_id]
            name=parse_qs(parsed.query)['name'][0]
            if any(asset['name']==name for asset in release['assets']):
                raise AssertionError('The transaction attempted to overwrite an existing asset')
            data=Path(args[args.index('--input')+1]).read_bytes()
            asset_id=max(self.contents,default=99)+1
            asset={'id':asset_id,'name':name,'size':len(data),'digest':'sha256:'+sha(data)}
            self.contents[asset_id]=data;release['assets'].append(asset)
            self.uploads.append((release_id,name))
            return json.dumps(asset)
        if method=='PATCH' and path.startswith(prefix+'/'):
            release=self.releases[int(path.rsplit('/',1)[1])]
            if 'draft=false' in args:release['draft']=False
            if 'draft=true' in args:release['draft']=True
            return json.dumps(release)
        raise AssertionError(args)

    def download_asset(self,asset,directory):
        self.downloads.append(asset['id'])
        (Path(directory)/asset['name']).write_bytes(self.contents[asset['id']])


class ManualRecovery(unittest.TestCase):
    version='v2.4.0'
    built_commit='a'*40

    def fixture(self,root,*,draft,existing=False,change_tag=False,mutation=None):
        work=root/'work';(work/'release').mkdir(parents=True);(work/'control').mkdir()
        archive=work/'release'/f'1panel-{self.version}-linux-amd64.tar.gz'
        archive.write_bytes(b'verified CI package bytes')
        checksum=archive.with_name(archive.name+'.sha256')
        checksum.write_text(sha(archive.read_bytes())+'  '+archive.name+'\n')
        files=[archive,checksum]
        expected={'files':{path.name:{'bytes':path.stat().st_size,'sha256':sha(path.read_bytes())} for path in files},
                  'outcomes':[{'architecture':'amd64','status':'success'}]}
        proof=dict(expected,upstream_input={'repository':manual.UPSTREAM,'run_id':10,'artifact_id':22,
                   'artifact_sha256':'b'*64,'build_repository_commit':self.built_commit})
        release={'id':17,'tag_name':self.version,'name':self.version,'draft':draft,'prerelease':False,
                 'body':'','target_commitish':self.built_commit,
                 'author':{'id':41898282,'login':'github-actions[bot]','type':'Bot'},'assets':[]}
        if existing:
            release['assets']=[{'id':100,'name':archive.name,'size':archive.stat().st_size,
                                'digest':'sha256:'+sha(archive.read_bytes())}]
        if mutation=='target':release['target_commitish']='c'*40
        if mutation=='author':release['author']={'id':5,'login':'someone','type':'User'}
        if mutation=='provenance':proof['upstream_input']['artifact_id']=0
        remote=RemoteRelease(self.version,release,files,change_tag=change_tag)
        if mutation=='bytes':
            # No trustworthy digest is available, so the fallback download
            # must detect corrupt bytes even when their size is unchanged.
            remote.releases[17]['assets'][0].pop('digest')
            remote.contents[100]=b'x'*len(remote.contents[100])
        proof_path=work/'control'/manual.PROOF
        proof_path.write_text(json.dumps(proof))
        args=SimpleNamespace(repository=manual.UPSTREAM,version=self.version,tag=self.version,work=work)
        return args,remote,files,expected,proof_path

    def publish(self,args,remote,files,expected,proof_path):
        with patch.object(manual,'GitHub',return_value=remote),\
             patch.object(manual,'validate_payloads',return_value=files),\
             patch.object(manual,'make_proof',return_value=expected),\
             patch('resolved_contract.activate_release',return_value=None),\
             patch.dict(os.environ,{'GITHUB_RUN_ID':'50','GITHUB_SHA':'d'*40,
                 'EXPECTED_VALIDATION_RECEIPT_SHA256':sha(proof_path.read_bytes())}),\
             patch('sys.stdout',new_callable=io.StringIO):
            return manual.publish(args)

    def test_public_repair_remains_on_the_validated_release_when_tag_resolution_moves(self):
        with tempfile.TemporaryDirectory() as temporary:
            args,remote,files,expected,proof=self.fixture(Path(temporary),draft=False,change_tag=True)
            self.publish(args,remote,files,expected,proof)
            self.assertEqual(remote.tag_id,18)
            self.assertEqual({name for release_id,name in remote.uploads if release_id==17},{path.name for path in files})
            self.assertEqual(remote.releases[18]['assets'],[])
            self.assertTrue(all(release_id==17 for release_id,_ in remote.uploads))
            self.assertEqual(remote.downloads,[])

    def test_public_gap_fill_preserves_other_assets_and_refuses_payload_conflicts(self):
        for conflict in (False,True):
            with self.subTest(conflict=conflict),tempfile.TemporaryDirectory() as temporary:
                args,remote,files,expected,proof_path=self.fixture(Path(temporary),draft=False)
                expected['outcomes'].append({'architecture':'arm64','status':'failure'})
                proof=json.loads(proof_path.read_text());proof['outcomes']=expected['outcomes']
                proof_path.write_text(json.dumps(proof))
                names=[f'1panel-{self.version}-linux-{arch}.tar.gz'+suffix
                       for arch in ('arm64','arm') for suffix in ('','.sha256')]
                names+=['build-manifest.json','old-helper.txt']
                for asset_id,name in enumerate(names,200):
                    raw=('preserve '+name).encode()
                    remote.contents[asset_id]=raw
                    # Non-payload attachments need no digest or readback.
                    remote.releases[17]['assets'].append({'id':asset_id,'name':name,'size':len(raw)})
                if conflict:
                    raw=b'existing different package'
                    remote.contents[300]=raw
                    remote.releases[17]['assets'].append({'id':300,'name':files[0].name,
                                                         'size':len(raw),'digest':'sha256:'+sha(raw)})
                before=copy.deepcopy(remote.releases[17]['assets'])
                if conflict:
                    with self.assertRaisesRegex(ValueError,'specific deletion confirmation'):
                        self.publish(args,remote,files,expected,proof_path)
                    self.assertEqual(remote.uploads,[])
                else:
                    self.publish(args,remote,files,expected,proof_path)
                    self.assertEqual(remote.uploads,[(17,path.name) for path in files])
                after={asset['id']:asset for asset in remote.releases[17]['assets']}
                for asset in before:self.assertEqual(after[asset['id']],asset)
                self.assertEqual(remote.downloads,[])

    def test_owned_draft_uses_server_digest_or_one_legacy_fallback_and_only_uploads_the_gap(self):
        for server_digest in (True,False):
            with self.subTest(server_digest=server_digest),tempfile.TemporaryDirectory() as temporary:
                args,remote,files,expected,proof=self.fixture(Path(temporary),draft=True,existing=True)
                if not server_digest:remote.releases[17]['assets'][0].pop('digest')
                self.publish(args,remote,files,expected,proof)
                self.assertEqual(remote.downloads,[] if server_digest else [100])
                self.assertEqual(remote.uploads,[(17,files[1].name)])
                self.assertFalse(remote.releases[17]['draft'])
                self.assertEqual(remote.releases[17]['target_commitish'],self.built_commit)
                self.assertEqual({asset['name'] for asset in remote.releases[17]['assets']},{path.name for path in files})

    def test_public_release_returned_to_draft_before_transaction_never_receives_uploads(self):
        with tempfile.TemporaryDirectory() as temporary:
            args,remote,files,expected,proof=self.fixture(Path(temporary),draft=False)
            transaction=manual.repair
            def changed_before_entry(client,*args,**kwargs):
                remote.releases[17]['draft']=True
                return transaction(client,*args,**kwargs)
            with patch.object(manual,'repair',side_effect=changed_before_entry):
                with self.assertRaisesRegex(ValueError,'Release changed before the bound repair transaction'):
                    self.publish(args,remote,files,expected,proof)
            self.assertEqual(remote.uploads,[])

    def test_unowned_mismatched_or_corrupt_drafts_never_upload_or_finalize(self):
        for mutation in ('target','author','bytes','provenance','target-after-discovery','target-before-repair'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as temporary:
                args,remote,files,expected,proof=self.fixture(Path(temporary),draft=True,existing=True,mutation=mutation)
                if mutation in ('target-after-discovery','target-before-repair'):
                    run=remote.run;reads=[0]
                    change_at=1 if mutation=='target-after-discovery' else 2
                    def changed_target(*args):
                        if args==('api',f'repos/{remote.repo}/releases/17'):
                            reads[0]+=1
                            if reads[0]==change_at:remote.releases[17]['target_commitish']='c'*40
                        return run(*args)
                    remote.run=changed_target
                with self.assertRaises(ValueError):self.publish(args,remote,files,expected,proof)
                self.assertEqual(remote.uploads,[])
                self.assertTrue(remote.releases[17]['draft'])
                self.assertFalse(any('draft=false' in call for call in remote.calls))


class VerifiedProducerRecovery(unittest.TestCase):
    def fixture(self,name):
        manifest,run,jobs=matrix_tests.MatrixContractTests().fixture()
        for job in jobs:
            job['conclusion']='success'
        manifest['outcomes']=matrix.collect(jobs,manifest['requested_architectures'],10,2)
        manifest['artifacts']=[{'architecture':arch,'build_repository_commit':'a'*40}
                               for arch in manifest['requested_architectures']]
        failed=dict(jobs[0],id=99,name=name,conclusion='failure')
        jobs.append(failed)
        return manifest,run,jobs

    def test_known_publication_failure_is_allowed_only_in_explicit_recovery_mode(self):
        for name in ('candidate','automatic_publication','publication_repair'):
            manifest,run,jobs=self.fixture(name)
            with self.subTest(name=name):
                with self.assertRaises(ValueError):matrix.verify_jobs(manifest,run,jobs,'a'*40)
                self.assertEqual(matrix.verify_jobs(manifest,run,jobs,'a'*40,allow_publication_failures=True),['amd64','arm64'])

    def test_failed_preparation_or_unknown_jobs_still_reject_recovery(self):
        for name in ('publication_prepare','unexpected-publish-step'):
            manifest,run,jobs=self.fixture(name)
            with self.subTest(name=name),self.assertRaises(ValueError):
                matrix.verify_jobs(manifest,run,jobs,'a'*40,allow_publication_failures=True)

    def test_manual_selection_can_fetch_the_verified_bundle_after_publication_failed(self):
        manifest,run,jobs=self.fixture('automatic_publication')
        manifest['version']='v2.4.0'
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);archive=root/'original.zip'
            with zipfile.ZipFile(archive,'w') as bundle:
                bundle.writestr('build-manifest.json',json.dumps(manifest))
            data=archive.read_bytes();digest=sha(data);commit='a'*40
            metadata={'expired':False,'workflow_run':{'id':10},
                      'name':f'verified-1panel-v2.4.0-{commit}','digest':'sha256:'+digest,'size_in_bytes':len(data)}
            def download(command,**kwargs):
                kwargs['stdout'].write(data)
                return SimpleNamespace(returncode=0)
            for status,conclusion in (('completed','failure'),('completed','cancelled'),('in_progress',None)):
                latest=dict(run,run_attempt=3,status=status,conclusion=conclusion)
                future=dict(jobs[-1],id=100,run_attempt=3,status=status,conclusion=conclusion)
                inventory=jobs+[future]
                with self.subTest(latest_status=status,latest_conclusion=conclusion),\
                     patch.object(manual,'github_json',side_effect=[latest,metadata,{'jobs':inventory,'total_count':len(inventory)},run]),\
                     patch.object(manual.subprocess,'run',side_effect=download):
                    provenance=manual.fetch_ci_bundle(root/('input-'+str(conclusion)),'v2.4.0','10','22',digest,commit,'upstream-matrix')
                self.assertEqual(provenance['build_repository_commit'],commit)
                self.assertEqual(provenance['artifact_sha256'],digest)
            # Only the selected producer's terminal state determines eligibility.
            with patch.object(manual,'github_json',side_effect=[run,metadata,{'jobs':jobs,'total_count':len(jobs)},dict(run,conclusion='cancelled')]),\
                 patch.object(manual.subprocess,'run',side_effect=download),\
                 self.assertRaisesRegex(ValueError,'terminal permitted build'):
                manual.fetch_ci_bundle(root/'cancelled-producer','v2.4.0','10','22',digest,commit,'upstream-matrix')


if __name__=='__main__':unittest.main()
