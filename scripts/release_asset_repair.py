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
    def __init__(self, repo, tag, release_id=None):
        self.repo=repo;self.tag=tag;self.release_id=None
        if release_id is not None:self.bind_release(release_id)
    def bind_release(self,release_id):
        if type(release_id) is not int or release_id<=0:raise ValueError('Invalid bound release ID')
        self.release_id=release_id
    def run(self,*args):return subprocess.run(['gh',*args],check=True,text=True,capture_output=True).stdout
    def release(self):
        endpoint=f'repos/{self.repo}/releases/'+(str(self.release_id) if self.release_id is not None else f'tags/{self.tag}')
        release=json.loads(self.run('api',endpoint))
        if self.release_id is not None and (release.get('id')!=self.release_id or release.get('tag_name')!=self.tag):
            raise ValueError('Bound release identity changed')
        return release
    def drafts(self):
        found=[]
        for page in range(1,101):
            rows=json.loads(self.run('api',f'repos/{self.repo}/releases?per_page=100&page={page}'))
            if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):raise ValueError('Invalid release inventory')
            found.extend(row for row in rows if row.get('tag_name')==self.tag and row.get('draft') is True)
            if len(rows)<100:return found
        raise ValueError('Excessive release inventory')
    def upload(self,path):
        if self.release_id is None:return self.run('release','upload',self.tag,str(path),'--repo',self.repo)
        endpoint=f'https://uploads.github.com/repos/{self.repo}/releases/{self.release_id}/assets?name={quote(Path(path).name,safe="")}'
        self.run('api','--method','POST',endpoint,'-H','Content-Type: application/octet-stream','--input',str(path))
    def download(self,name,directory):
        if self.release_id is None:return self.run('release','download',self.tag,'--repo',self.repo,'--pattern',name,'--dir',str(directory))
        matches=[asset for asset in self.release()['assets'] if asset['name']==name]
        if len(matches)!=1:raise ValueError('Bound release asset is missing or ambiguous')
        self.download_asset(matches[0],directory)
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

def optional_release(client):
    try:return client.release()
    except subprocess.CalledProcessError as error:
        # A tag lookup excludes drafts. Only an explicit not-found permits
        # draft discovery; transport and permission errors must not create one.
        if error.returncode==1 and '(HTTP 404)' in (error.stderr or ''):
            drafts=client.drafts()
            if len(drafts)>1:raise ValueError('Multiple matching release drafts require recovery review')
            return drafts[0] if drafts else None
        raise

def require_owned_draft(release,client):
    author=release.get('author') or {}
    if (type(release.get('id')) is not int or release['id']<=0 or
        release.get('draft') is not True or release.get('tag_name')!=client.tag or
        release.get('name')!=client.tag or release.get('prerelease') is not ('-' in client.tag) or
        (release.get('body') or '')!='' or
        {key:author.get(key) for key in ('id','login','type')}!=
            {'id':41898282,'login':'github-actions[bot]','type':'Bot'}):
        raise ValueError('Only an owned automatic release draft can be recovered')

def verify_new_release(release,created,files,draft):
    if (release.get('id')!=created['id'] or release.get('tag_name')!=created['tag_name'] or
        release.get('draft') is not draft or (release.get('body') or '')!=(created.get('body') or '')):
        raise ValueError('Created release identity or visibility changed')
    if any(release.get(key)!=created.get(key) for key in ('name','target_commitish','prerelease','author')):
        raise ValueError('Created release metadata changed')
    expected={p.name:digest(p) for p in files}
    assets=release.get('assets',[])
    if len(assets)!=len(expected) or {a['name'] for a in assets}!=set(expected):
        raise ValueError('Created release canonical inventory changed')
    for asset in assets:
        facts=expected[asset['name']]
        if asset.get('digest')!='sha256:'+facts['sha256'] or asset.get('size')!=facts['bytes']:
            raise ValueError('Created release canonical bytes changed')

def finalize_release(client,created,files,creation):
    """Publish one verified owned draft, retaining its exact ID on recovery."""
    verify_new_release(client.release(),created,files,True)
    creation.state['phase']='finalizing';creation.save()
    try:
        client.run('api','--method','PATCH',f'repos/{client.repo}/releases/{created["id"]}',
                   '-F','draft=false','-f','make_latest=legacy')
        verify_new_release(client.release(),created,files,False)
    except Exception:
        # A failed response can follow an applied publication. Return only the
        # exact owned release to draft; never infer that the PATCH did nothing.
        try:
            client.run('api','--method','PATCH',f'repos/{client.repo}/releases/{created["id"]}','-F','draft=true')
            restored=json.loads(client.run('api',f'repos/{client.repo}/releases/{created["id"]}'))
            if restored.get('id')!=created['id'] or restored.get('draft') is not True:raise ValueError('Draft recovery readback failed')
            creation.state['phase']='returned_to_draft';creation.save()
        except Exception as recovery_error:
            creation.state.update(phase='recovery_required',recovery_error=str(recovery_error));creation.save()
        raise
    creation.state['phase']='complete';creation.save()

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
            # Public notes are editable prose; only owned draft transactions
            # bind their exact text. No append operation writes public notes.
            'body':(release.get('body') or '') if release.get('draft') is True else '',
            'assets':sorted([{'id':a['id'],'name':a['name'],'size':a['size'],'digest':a.get('digest')} for a in release['assets']],key=lambda a:a['id'])}

def draft_metadata(release):
    return {**{key:release.get(key) for key in ('id','tag_name','draft','name','target_commitish','prerelease')},
            'body':release.get('body') or '',
            'author_id':(release.get('author') or {}).get('id')}

def repair(client, files, journal_path, retire=(), *, expected_snapshot=None, preserved_assets=None, expected_draft=None):
    before=client.release()
    if expected_snapshot is not None and release_snapshot(before)!=expected_snapshot:
        raise ValueError('Release changed before the bound repair transaction')
    if expected_draft is not None and draft_metadata(before)!=draft_metadata(expected_draft):
        raise ValueError('Draft release metadata changed before the bound repair transaction')
    assets={a['name']:a for a in before['assets']}
    if len(assets)!=len(before['assets']):raise ValueError('Duplicate remote asset names')
    expected={p.name:digest(p) for p in files}
    if len(expected)!=len(files):raise ValueError('Duplicate local publication filenames')
    if set(retire) & set(expected) or len(set(retire)) != len(retire):raise ValueError('Invalid retired artifact names')
    allowed=set(expected)|set(retire)
    if before.get('draft') is True and set(assets)-set(expected):
        raise ValueError('Draft recovery accepts only the selected validated package files')
    preserved_assets=preserved_assets or {}
    if set(preserved_assets)&allowed:raise ValueError('Preserved recovery inventory overlaps canonical mutation')
    for name,asset in preserved_assets.items():
        if not assets.get(name) or asset_identity(assets[name])!=asset_identity(asset):raise ValueError('Preserved recovery asset changed before transaction')
    check_asset_names(before['assets'],allowed,preserved_assets)
    state={'repo':client.repo,'tag':client.tag,'phase':'preflight','conflicts':[],'uploads':[]}
    journal=Journal(journal_path,state)
    known={name:asset_identity(asset) for name,asset in assets.items()}
    draft_baseline=draft_metadata(expected_draft or before) if before.get('draft') is True else None
    identity={key:before.get(key) for key in ('id','tag_name','draft')}
    def inventory():
        release=client.release()
        if draft_baseline is not None and draft_metadata(release)!=draft_baseline:
            raise ValueError('Draft release metadata changed during publication')
        if any(release.get(key)!=value for key,value in identity.items()):
            raise ValueError('Release identity or visibility changed during publication')
        rows=release['assets']
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
                if not old_sha or draft_baseline is not None:
                    readback=temp/str(old['id']);readback.mkdir()
                    client.download_asset(old,readback)
                    facts=digest(readback/name)
                    if facts['bytes']!=old['size']:raise ValueError('Original asset failed readback verification')
                    if old_sha and old_sha!='sha256:'+facts['sha256']:
                        raise ValueError('Existing draft asset differs from its server digest')
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
