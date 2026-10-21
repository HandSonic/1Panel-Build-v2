"""Exercise the real GitHub adapter's draft and ID-based API routing."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from release_asset_repair import GitHub, repair


class GitHubReleaseClientTests(unittest.TestCase):
    repo='owner/repo'
    tag='v2.4.0'

    def result(self,value):
        return subprocess.CompletedProcess([],0,json.dumps(value),'')

    def test_binding_accepts_only_a_positive_integer(self):
        for value in (True,False,0,-1,'17',1.5,None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                GitHub(self.repo,self.tag).bind_release(value)
        client=GitHub(self.repo,self.tag,17)
        self.assertEqual(client.release_id,17)

    def test_unbound_tag_lookup_404_does_not_hide_existing_draft(self):
        draft={'id':17,'tag_name':self.tag,'draft':True,'assets':[]}
        def api(command,**kwargs):
            self.assertEqual(kwargs,dict(check=True,text=True,capture_output=True))
            if command==['gh','api',f'repos/{self.repo}/releases/tags/{self.tag}']:
                raise subprocess.CalledProcessError(1,command,stderr='gh: Not Found (HTTP 404)')
            self.assertEqual(command,['gh','api',f'repos/{self.repo}/releases?per_page=100&page=1'])
            return self.result([draft,dict(draft,id=18,draft=False),dict(draft,id=19,tag_name='v1.0.0')])
        client=GitHub(self.repo,self.tag)
        with patch('release_asset_repair.subprocess.run',side_effect=api):
            with self.assertRaises(subprocess.CalledProcessError):client.release()
            self.assertEqual(client.drafts(),[draft])

    def test_bound_release_uses_id_while_still_a_draft(self):
        draft={'id':17,'tag_name':self.tag,'draft':True,'assets':[]}
        with patch('release_asset_repair.subprocess.run',return_value=self.result(draft)) as run:
            self.assertEqual(GitHub(self.repo,self.tag,17).release(),draft)
        run.assert_called_once_with(['gh','api',f'repos/{self.repo}/releases/17'],check=True,text=True,capture_output=True)

    def test_bound_release_rejects_id_or_tag_drift(self):
        for release in ({'id':18,'tag_name':self.tag},{'id':17,'tag_name':'v1.0.0'}):
            with self.subTest(release=release),patch('release_asset_repair.subprocess.run',return_value=self.result(release)):
                with self.assertRaisesRegex(ValueError,'identity changed'):
                    GitHub(self.repo,self.tag,17).release()

    def test_drafts_scan_every_page_and_only_return_matching_drafts(self):
        filler={'id':1,'tag_name':'v1.0.0','draft':False}
        draft={'id':17,'tag_name':self.tag,'draft':True}
        pages=[[filler]*99+[draft],[dict(draft,id=18)]]
        with patch('release_asset_repair.subprocess.run',side_effect=[self.result(rows) for rows in pages]) as run:
            self.assertEqual(GitHub(self.repo,self.tag).drafts(),[draft,dict(draft,id=18)])
        self.assertEqual([call.args[0][-1] for call in run.call_args_list],
            [f'repos/{self.repo}/releases?per_page=100&page={page}' for page in (1,2)])

    def test_draft_inventory_rejects_invalid_or_excessive_pages(self):
        for rows in ({'message':'bad response'},['invalid release']):
            with self.subTest(rows=rows),patch('release_asset_repair.subprocess.run',return_value=self.result(rows)):
                with self.assertRaisesRegex(ValueError,'Invalid release inventory'):
                    GitHub(self.repo,self.tag).drafts()
        with patch('release_asset_repair.subprocess.run',return_value=self.result([{}]*100)) as run:
            with self.assertRaisesRegex(ValueError,'Excessive release inventory'):
                GitHub(self.repo,self.tag).drafts()
        self.assertEqual(run.call_count,100)

    def test_bound_upload_uses_id_and_encodes_the_asset_basename(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'package + #.tar.gz'
            path.write_bytes(b'package bytes')
            with patch('release_asset_repair.subprocess.run',return_value=self.result({})) as run:
                GitHub(self.repo,self.tag,17).upload(path)
            run.assert_called_once_with(['gh','api','--method','POST',
                f'https://uploads.github.com/repos/{self.repo}/releases/17/assets?name=package%20%2B%20%23.tar.gz',
                '-H','Content-Type: application/octet-stream','--input',str(path)],
                check=True,text=True,capture_output=True)

    def test_bound_download_reads_binary_bytes_through_asset_id_api(self):
        asset={'id':81,'name':'package.tar.gz'}
        release={'id':17,'tag_name':self.tag,'draft':True,'assets':[asset]}
        calls=[]
        def api(command,**kwargs):
            calls.append(command)
            if 'stdout' in kwargs:
                self.assertEqual(command,['gh','api',f'repos/{self.repo}/releases/assets/81','-H','Accept: application/octet-stream'])
                kwargs['stdout'].write(b'package\x00bytes')
                return subprocess.CompletedProcess(command,0)
            return self.result(release)
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            with patch('release_asset_repair.subprocess.run',side_effect=api):
                GitHub(self.repo,self.tag,17).download(asset['name'],directory)
            self.assertEqual((directory/asset['name']).read_bytes(),b'package\x00bytes')
        self.assertEqual(len(calls),2)

    def test_bound_download_rejects_missing_or_duplicate_names(self):
        asset={'id':81,'name':'package.tar.gz'}
        client=GitHub(self.repo,self.tag,17)
        for assets in ([],[asset,dict(asset,id=82)]):
            release={'id':17,'tag_name':self.tag,'assets':assets}
            with self.subTest(assets=assets),patch('release_asset_repair.subprocess.run',return_value=self.result(release)),patch.object(client,'download_asset') as download:
                with self.assertRaisesRegex(ValueError,'missing or ambiguous'):
                    client.download(asset['name'],Path('unused'))
                download.assert_not_called()

    def test_unbound_upload_and_download_keep_existing_cli_behavior(self):
        client=GitHub(self.repo,self.tag)
        with patch('release_asset_repair.subprocess.run',return_value=self.result({})) as run:
            client.upload(Path('package.tar.gz'))
            client.download('package.tar.gz',Path('destination'))
        self.assertEqual([call.args[0] for call in run.call_args_list],[
            ['gh','release','upload',self.tag,'package.tar.gz','--repo',self.repo],
            ['gh','release','download',self.tag,'--repo',self.repo,'--pattern','package.tar.gz','--dir','destination']])

    def test_real_repair_stops_before_upload_after_draft_metadata_drift(self):
        before={'id':17,'tag_name':self.tag,'draft':True,'body':'','name':self.tag,
                'target_commitish':'a'*40,'prerelease':False,'author':{'id':41898282},'assets':[]}
        changes={'draft':False,'body':'external notes','name':'external name',
                 'target_commitish':'b'*40,'prerelease':True,'author':{'id':5}}
        for field,value in changes.items():
            changed=copy.deepcopy(before);changed[field]=value
            with self.subTest(field=field),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);package=root/'package.tar.gz';package.write_bytes(b'package')
                client=GitHub(self.repo,self.tag,17)
                with patch('release_asset_repair.subprocess.run',side_effect=[self.result(before),self.result(changed)]) as run:
                    with self.assertRaisesRegex(ValueError,'Draft release metadata changed'):
                        repair(client,[package],root/'journal.json')
                self.assertEqual(run.call_count,2)
                self.assertEqual(json.loads((root/'journal.json').read_text())['phase'],'incomplete')

    def test_real_repair_stops_if_metadata_changes_with_the_first_upload(self):
        before={'id':17,'tag_name':self.tag,'draft':True,'body':'','name':self.tag,
                'target_commitish':'a'*40,'prerelease':False,'author':{'id':41898282},'assets':[]}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);package=root/'package.tar.gz';package.write_bytes(b'package')
            changed=copy.deepcopy(before);changed['draft']=False
            client=GitHub(self.repo,self.tag,17)
            with patch('release_asset_repair.subprocess.run',side_effect=[self.result(before),self.result(before),self.result(changed)]) as run,patch.object(client,'upload') as upload:
                with self.assertRaisesRegex(ValueError,'Draft release metadata changed'):
                    repair(client,[package],root/'journal.json')
            upload.assert_called_once_with(package)
            self.assertEqual(run.call_count,3)
            self.assertFalse(any('PATCH' in call.args[0] for call in run.call_args_list))

    def test_public_release_repair_keeps_existing_metadata_behavior(self):
        before={'id':17,'tag_name':self.tag,'draft':False,'body':'original','assets':[]}
        changed=dict(before,body='external notes')
        client=GitHub(self.repo,self.tag,17)
        with tempfile.TemporaryDirectory() as temporary,patch('release_asset_repair.subprocess.run',side_effect=[self.result(before),self.result(changed)]):
            self.assertEqual(repair(client,[],Path(temporary)/'journal.json')['phase'],'complete')


if __name__=='__main__':unittest.main()
