"""Exercise candidate creation and the real ID-bound upload/readback transaction."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import candidate_publication as candidate
from test_draft_publication import TransportGitHub


class CandidateTransport(TransportGitHub):
    def __init__(self,args,*,existing=None,lookup_error=None,drift=False):
        super().__init__(existing)
        self.tag=args.tag;self.target=args.target
        self.lookup_error=lookup_error;self.drift=drift
        self.tag_id=17 if existing else None
        self.upload_ids=[];self.download_ids=[]

    def run(self,*args):
        if args==('api',f'repos/{self.repo}/releases/tags/{self.tag}'):
            if self.lookup_error is not None:raise self.lookup_error
            if self.tag_id==18:
                raise AssertionError('Candidate reparsed a tag after creation instead of using ID 17')
        if args[:4]==('api','--method','POST',f'repos/{self.repo}/releases'):
            self.calls.append(args)
            self.value={'id':17,'tag_name':self.tag,'name':self.tag,'draft':True,
                'prerelease':False,'body':candidate.NOTES,'target_commitish':self.target,
                'author':{'id':41898282,'login':'github-actions[bot]','type':'Bot'},'assets':[]}
            self.tag_id=18  # A concurrent replacement now owns tag-based lookups.
            return json.dumps(self.value)
        if args==('api',f'repos/{self.repo}/releases/17') and self.drift:
            self.value['target_commitish']='c'*40
        if args[:3]==('api','--method','POST') and args[3].startswith('https://uploads.github.com/'):
            self.upload_ids.append(self.release_id)
        return super().run(*args)

    def download_asset(self,asset,directory):
        self.download_ids.append(self.release_id)
        return super().download_asset(asset,directory)


class CandidatePublication(unittest.TestCase):
    def fixture(self,root):
        version='v2.4.0';target='a'*40;run_id=123
        source=root/'dist';source.mkdir()
        name=f'1panel-{version}-linux-amd64.tar.gz'
        (source/name).write_bytes(b'verified candidate package')
        (source/(name+'.sha256')).write_bytes(b'verified candidate sidecar')
        manifest={'schema_version':2,'version':version,'artifacts':[{'architecture':'amd64'}],
            'requested_architectures':['amd64'],'producer_run_id':run_id,'producer_run_attempt':1,
            'producer_head_sha':target,'outcomes':[{'architecture':'amd64','status':'success','reason':'',
            'stage':'compile','job_id':77,'job_url':f'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/{run_id}/job/77'}]}
        (source/'build-manifest.json').write_text(json.dumps(manifest))
        return SimpleNamespace(repository='HandSonic/1Panel-Build-v2',version=version,
            tag=f'{version}-build-{target[:12]}-{run_id}',target=target,input=source,control=root/'candidate-control')

    def test_tag_replacement_never_redirects_candidate_upload_or_readback(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'GITHUB_RUN_ID':'123'}):
            args=self.fixture(Path(temporary));remote=CandidateTransport(args)
            result=candidate.publish(args,remote)
            self.assertEqual(remote.tag_id,18)
            self.assertEqual(remote.upload_ids,[17,17])
            self.assertEqual(remote.download_ids,[17,17])
            self.assertTrue(remote.value['draft'])
            self.assertEqual(remote.value['body'],candidate.NOTES)
            self.assertEqual(result,{'state':'verified_draft','tag':args.tag,'release_id':17})
            journal=json.loads((args.control/'creation-journal.json').read_text())
            self.assertEqual((journal['release_id'],journal['phase']),(17,'complete'))
            self.assertFalse(any('PATCH' in call for call in remote.calls))

    def test_lookup_failure_or_existing_candidate_never_creates_a_release(self):
        for condition in ('http500','existing'):
            with self.subTest(condition=condition),tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'GITHUB_RUN_ID':'123'}):
                args=self.fixture(Path(temporary))
                error=subprocess.CalledProcessError(1,['gh','api'],stderr='gh: error (HTTP 500)')
                existing={'id':17,'tag_name':args.tag,'draft':True} if condition=='existing' else None
                remote=CandidateTransport(args,existing=existing,lookup_error=error if condition=='http500' else None)
                with self.assertRaises((ValueError,subprocess.CalledProcessError)):
                    candidate.publish(args,remote)
                self.assertFalse(any('POST' in call for call in remote.calls))
                self.assertFalse(args.control.exists())

    def test_changed_draft_metadata_is_rejected_before_any_asset_upload(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'GITHUB_RUN_ID':'123'}):
            args=self.fixture(Path(temporary));remote=CandidateTransport(args,drift=True)
            with self.assertRaisesRegex(ValueError,'metadata changed'):
                candidate.publish(args,remote)
            self.assertEqual(remote.upload_ids,[])
            self.assertEqual(remote.value['assets'],[])
            journal=json.loads((args.control/'creation-journal.json').read_text())
            self.assertEqual((journal['release_id'],journal['phase']),(17,'incomplete'))


if __name__=='__main__':unittest.main()
