#!/usr/bin/env bash
# Isolated downloader regression checks; no network, Docker or root required.
set -euo pipefail
resource_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/download_resources.sh"
python3 - "$resource_script" <<'PY'
import json, os, pathlib, shutil, subprocess, sys, tarfile, tempfile
script = sys.argv[1]
with tempfile.TemporaryDirectory(prefix='1panel-resource-test-') as temp:
    root = pathlib.Path(temp)
    fixture = root / 'installer-v2'
    (fixture / 'lang').mkdir(parents=True)
    (fixture / 'initscript').mkdir()
    (fixture / '1pctl').write_text('#!/bin/bash\necho control\n')
    (fixture / 'install.sh').write_text('#!/bin/bash\nAVAILABLE_LANGS=("new-locale")\ncp ./GeoIP.mmdb /tmp/test-destination\n')
    # A previously unknown language and init system must arrive automatically.
    (fixture / 'lang/new-locale.sh').write_text('TXT_HELLO="hello"\n')
    (fixture / 'lang/unused-broken.sh').write_text('<html>not a language</html>')
    for kind in ('core', 'agent'):
        (fixture / f'initscript/1panel-{kind}.service').write_text('[Service]\nExecStart=/usr/bin/1panel\n')
    (fixture / 'initscript/1panel-new-system').write_text('#!/bin/sh\necho new\n')
    (root / 'GeoIP.mmdb').write_bytes(b'fixture-data\xab\xcd\xefMaxMind.commetadata')
    archive = root / 'installer.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(fixture, arcname='installer-v2')
    tree = {'tree': [{'path': str(p.relative_to(fixture)), 'type': 'blob'}
                     for p in fixture.rglob('*') if p.is_file()]}
    (root / 'tree.json').write_text(json.dumps(tree))
    mockbin = root / 'mockbin'
    mockbin.mkdir()
    curl = mockbin / 'curl'
    curl.write_text('''#!/usr/bin/env python3
import os,pathlib,shutil,sys
args=sys.argv[1:]
out=pathlib.Path(args[args.index('--output')+1])
url=next(a for a in args if a.startswith('https://'))
root=pathlib.Path(os.environ['TEST_ROOT'])
mode=os.environ['TEST_MODE']
with open(os.environ['TEST_LOG'],'a') as f:f.write(url+'\\n')
if mode=='offline':sys.exit(22)
if 'GeoIP.mmdb' in url:
    if mode=='bad_geo' or 'fit2cloud' in url:out.write_text('<html>proxy error</html>')
    else:shutil.copyfile(root/'GeoIP.mmdb',out)
elif mode=='html':out.write_text('<html>proxy error</html>')
elif 'codeload.github.com' in url:
    out.write_text('partial')
    sys.exit(22)
elif '/archive/' in url:
    if mode in ('tree','raw_html'):sys.exit(22)
    shutil.copyfile(root/'installer.tar.gz',out)
elif 'api.github.com' in url:shutil.copyfile(root/'tree.json',out)
else:
    prefix='https://raw.githubusercontent.com/1Panel-dev/installer/v2/'
    if url.startswith(prefix):
        if mode=='raw_html':out.write_text('<html>proxy error</html>')
        else:shutil.copyfile(root/'installer-v2'/url[len(prefix):],out)
    else:
        prefix='https://github.com/1Panel-dev/installer/raw/v2/'
        if not url.startswith(prefix):sys.exit(22)
        shutil.copyfile(root/'installer-v2'/url[len(prefix):],out)
''')
    curl.chmod(0o755)
    git = mockbin / 'git'
    git.write_text('#!/bin/sh\nexit 1\n')
    git.chmod(0o755)
    def run(name, mode, success=True, existing=None, ref='v2'):
        work = existing or root / name
        work.mkdir(exist_ok=True)
        logfile = root / (name + '.log')
        env = dict(os.environ, PATH=str(mockbin)+os.pathsep+os.environ['PATH'],
                   TMPDIR=str(root), TEST_ROOT=str(root), TEST_MODE=mode, TEST_LOG=str(logfile), INSTALLER_REF=ref)
        result = subprocess.run(['bash',script], cwd=work, env=env, text=True, capture_output=True)
        assert (result.returncode==0)==success, f'{name}: {result.stdout}\n{result.stderr}'
        print('PASS',name)
        return work, logfile
    work, log = run('archive-fallback-and-geoip-validation','archive_fallback')
    assert (work/'lang/new-locale.sh').is_file()
    assert (work/'initscript/1panel-new-system').is_file()
    assert not (work/'initscript/1panel-core.openrc').exists()
    assert (work/'GeoIP.mmdb').read_bytes()==(root/'GeoIP.mmdb').read_bytes()
    _, log = run('validated-cache-during-outage','offline',existing=work)
    assert not log.exists(), 'valid cache should make no download attempts'
    (work/'lang/new-locale.sh').write_text('<html>corrupt cache</html>')
    run('invalid-cache-repaired','archive_fallback',existing=work)
    assert (work/'lang/new-locale.sh').read_text().startswith('TXT_HELLO')
    run('tree-fallback-dynamic-inventory','tree')
    run('raw-html-falls-back-to-second-endpoint','raw_html')
    assert not (work/'lang/unused-broken.sh').exists()
    os.utime(work/'.installer-resource-manifest',(0,0))
    (fixture/'lang/later-added.sh').write_text('TXT_NEW="new"\n')
    tree['tree'].append({'path':'lang/later-added.sh','type':'blob'})
    (root/'tree.json').write_text(json.dumps(tree))
    run('expired-cache-discovers-upstream-additions','tree',existing=work)
    assert (work/'lang/later-added.sh').is_file()
    legacy = root/'legacy'
    shutil.copytree(fixture,legacy)
    shutil.copyfile(root/'GeoIP.mmdb',legacy/'GeoIP.mmdb')
    run('legacy-cache-during-outage','offline',existing=legacy)
    run('required-resources-absent','offline',success=False)
    run('html-responses-rejected','html',success=False)
    run('invalid-geoip-rejected','bad_geo',success=False)
    run('requested-ref-must-not-use-other-ref-cache','offline',success=False,existing=work,ref='different-ref')
    (legacy/'lang/new-locale.sh').unlink()
    run('menu-language-missing-is-fatal','offline',success=False,existing=legacy)
print('All isolated resource tests passed.')
PY
