#!/usr/bin/env python3
"""Publish absent assets; unchanged assets are no-ops, replacements require review."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
import hashlib
from urllib.parse import quote

def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):h.update(chunk)
    return {"bytes":Path(path).stat().st_size,"sha256":h.hexdigest()}

class GitHub:
    def __init__(self, repo, tag):self.repo=repo;self.tag=tag;self.release_id=None
    def run(self,*args):return subprocess.run(['gh',*args],check=True,text=True,capture_output=True).stdout
    def bind_release(self,release_id):
        if type(release_id) is not int or release_id<=0:raise ValueError('Invalid bound release ID')
        if self.release_id not in (None,release_id):raise ValueError('Bound release identity changed')
        self.release_id=release_id
    def release(self):
        endpoint=f'repos/{self.repo}/releases/'
        endpoint+=str(self.release_id) if self.release_id is not None else 'tags/'+self.tag
        release=json.loads(self.run('api',endpoint))
        if (type(release.get('id')) is not int or release['id']<=0 or release.get('tag_name')!=self.tag or
                (self.release_id is not None and release['id']!=self.release_id)):
            raise ValueError('Release identity mismatch')
        return release
    def find_draft(self):
        """Explicit writer-only discovery; ordinary source reads stay public/tag based."""
        matches=[];seen=set()
        for page in range(1,101):
            rows=json.loads(self.run('api',f'repos/{self.repo}/releases?per_page=100&page={page}'))
            if not isinstance(rows,list) or len(rows)>100:raise ValueError('Invalid release discovery page')
            for release in rows:
                release_id=release.get('id') if isinstance(release,dict) else None
                if type(release_id) is not int or release_id<=0 or release_id in seen:
                    raise ValueError('Invalid or duplicated discovered release identity')
                seen.add(release_id)
                if release.get('tag_name')==self.tag:matches.append(release)
            if len(rows)<100:break
        else:raise ValueError('Release discovery page limit exceeded')
        if not matches:return None
        if len(matches)!=1 or matches[0].get('draft') is not True:
            raise ValueError('Ambiguous or non-draft release discovery')
        self.bind_release(matches[0]['id'])
        release=self.release()
        if release_snapshot(release)!=release_snapshot(matches[0]):
            raise ValueError('Draft changed during release discovery')
        return release
    def upload(self,path):
        if self.release_id is None:
            return self.run('release','upload',self.tag,str(path),'--repo',self.repo)
        path=Path(path)
        endpoint=f'https://uploads.github.com/repos/{self.repo}/releases/{self.release_id}/assets?name='+quote(path.name,safe='')
        return self.run('api','--method','POST',endpoint,'-H','Content-Type: application/octet-stream',
                        '-H',f'Content-Length: {path.stat().st_size}','--input',str(path))
    def download(self,name,directory):
        if self.release_id is None:
            return self.run('release','download',self.tag,'--repo',self.repo,'--pattern',name,'--dir',str(directory))
        matches=[asset for asset in self.release()['assets'] if asset.get('name')==name]
        if len(matches)!=1:raise ValueError('Missing or ambiguous bound release asset')
        return self.download_asset(matches[0],directory)
    def download_asset(self,asset,directory):
        with (Path(directory)/asset['name']).open('wb') as stream:
            subprocess.run(['gh','api',f"repos/{self.repo}/releases/assets/{asset['id']}",'-H','Accept: application/octet-stream'],check=True,stdout=stream)
    def notes(self,body):self.run('release','edit',self.tag,'--repo',self.repo,'--notes',body)

class Journal:
    def __init__(self,path,state):self.path=Path(path);self.state=state;self.save()
    def save(self):
        temporary=self.path.with_name(self.path.name+'.part')
        with temporary.open('w') as stream:
            json.dump(self.state,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        temporary.replace(self.path)

def check_asset_names(assets,expected,extra=()):
    names=[a['name'] for a in assets]
    if len(names)!=len(set(names)):raise ValueError('Duplicate remote asset names')
    for name in set(names)-set(expected)-set(extra):
        if not any(re.fullmatch(re.escape(base)+r'\.(backup|staged)-[0-9a-f]{12}',name) for base in expected):
            raise ValueError(f'Unexpected canonical asset requires review: {name}')

def asset_identity(asset):
    return {key:asset.get(key) for key in ('id','name','size','digest')}

def release_snapshot(release):
    if release is None:return None
    return {'id':release['id'],'draft':release['draft'],'tag_name':release['tag_name'],
            'body':release.get('body') or '',
            'assets':sorted([{'id':a['id'],'name':a['name'],'size':a['size'],'digest':a.get('digest')} for a in release['assets']],key=lambda a:a['id'])}


def repair(client, files, journal_path, retire=(), *, expected_snapshot=None, preserved_assets=None):
    before=client.release()
    if expected_snapshot is not None and release_snapshot(before)!=expected_snapshot:
        raise ValueError('Release changed before the bound repair transaction')
    assets={a['name']:a for a in before['assets']}
    if len(assets)!=len(before['assets']):raise ValueError('Duplicate remote asset names')
    expected={p.name:digest(p) for p in files}
    if len(expected)!=len(files):raise ValueError('Duplicate local publication filenames')
    if set(retire) & set(expected) or len(set(retire)) != len(retire):raise ValueError('Invalid retired artifact names')
    allowed=set(expected)|set(retire)
    preserved_assets=preserved_assets or {}
    if set(preserved_assets)&allowed:raise ValueError('Preserved recovery inventory overlaps canonical mutation')
    for name,asset in preserved_assets.items():
        if not assets.get(name) or asset_identity(assets[name])!=asset_identity(asset):raise ValueError('Preserved recovery asset changed before transaction')
    check_asset_names(before['assets'],allowed,preserved_assets)
    state={'repo':client.repo,'tag':client.tag,'phase':'preflight','conflicts':[],'uploads':[]}
    journal=Journal(journal_path,state)
    known={name:asset_identity(asset) for name,asset in assets.items()}
    def inventory():
        rows=client.release()['assets']
        result={a['name']:asset_identity(a) for a in rows}
        if len(result)!=len(rows):raise ValueError('Duplicate remote asset names')
        return result
    try:
        # Inspect the whole request before the first upload. No rename, clobber or delete.
        with tempfile.TemporaryDirectory() as temporary:
            temp=Path(temporary)
            for name in sorted(allowed):
                old=assets.get(name)
                if old is None:continue
                old_sha=old.get('digest')
                if not old_sha:
                    readback=temp/str(old['id']);readback.mkdir()
                    client.download_asset(old,readback)
                    facts=digest(readback/name)
                    if facts['bytes']!=old['size']:raise ValueError('Original asset failed readback verification')
                    old_sha='sha256:'+facts['sha256']
                    (readback/name).unlink()
                replacement=expected.get(name)
                if replacement and old_sha=='sha256:'+replacement['sha256'] and old['size']==replacement['bytes']:
                    continue
                state['conflicts'].append({**asset_identity(old),'digest':old_sha,
                    'reason':'retirement' if name in retire else 'different_content',
                    'replacement':replacement})
            if state['conflicts']:
                state['phase']='confirmation_required';journal.save()
                raise ValueError('Existing assets require specific deletion confirmation; see publication journal. No assets changed.')
            state['phase']='uploading';journal.save()
            for source in files:
                if inventory()!=known:raise ValueError('Release asset inventory changed during publication')
                if source.name in known:continue
                facts=expected[source.name]
                state['pending']={'name':source.name,**facts};journal.save()
                client.upload(source)
                fresh=inventory()
                uploaded=fresh.get(source.name)
                if not uploaded or uploaded['digest']!='sha256:'+facts['sha256'] or uploaded['size']!=facts['bytes']:
                    raise ValueError('Uploaded server digest/size mismatch')
                if {name:row for name,row in fresh.items() if name!=source.name}!=known:
                    raise ValueError('Release asset inventory changed during upload')
                readback=temp/('uploaded-'+str(uploaded['id']));readback.mkdir()
                client.download(source.name,readback)
                if digest(readback/source.name)!=facts:raise ValueError('Uploaded bytes failed download verification')
                (readback/source.name).unlink()
                known=fresh;state['uploads'].append(uploaded);state.pop('pending',None);journal.save()
            if inventory()!=known:raise ValueError('Release asset inventory changed before final verification')
            state['phase']='complete';journal.save()
    except Exception as error:
        state['failure']=str(error)
        if state['phase']!='confirmation_required':state['phase']='incomplete'
        # Successfully uploaded canonical files remain usable. A retry fills only gaps.
        # Never delete or rename assets, including after an uncertain upload response.
        journal.save()
        raise
    return state
