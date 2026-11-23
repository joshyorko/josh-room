import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from josh_room import homebrew_prefix as brew
archive=Path('/task/component-from-store.tar.zst')
prefix=Path(brew.PREFIX)
assert not os.path.lexists(prefix)
result={'prefix_initially_absent':True,'network':'none','root_readonly':True}
def cli(*args,ok=True):
    run=subprocess.run([sys.executable,'-m','josh_room',*args,'--json'],capture_output=True,text=True)
    body=json.loads(run.stdout)
    assert (run.returncode==0)==ok,body
    return body
plan=cli('homebrew','plan-restore','--archive',str(archive),'--prefix',str(prefix))
result['plan']=plan
result['restore']=cli('homebrew','restore','--archive',str(archive),'--prefix',str(prefix),'--confirm-manifest-sha256',plan['manifest_sha256'])
manifest,data=brew.read_archive(archive)
expected=set(manifest['files'])|set(manifest['links'])|set(manifest['directories'])
for name in list(expected):
    expected.update(str(p) for p in PurePosixPath(name).parents if str(p)!='.')
actual=set()
for root,dirs,files in os.walk(prefix,followlinks=False):
    for name in dirs+files: actual.add((Path(root)/name).relative_to(prefix).as_posix())
assert expected==actual,{'missing':list(expected-actual),'extra':list(actual-expected)}
for name,record in manifest['files'].items():
    path=prefix/name
    assert not path.is_symlink()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256']
    assert path.stat().st_mode&0o777==record['mode']
for name,target in manifest['links'].items():
    assert (prefix/name).readlink().as_posix()==target
    assert (prefix/name).resolve().is_relative_to(prefix)
result.update(files_verified=len(data),links_verified=len(manifest['links']),topology_entries=len(actual),no_unrelated_files=True,all_file_modes_and_digests_match=True)
env={k:v for k,v in os.environ.items() if k not in ('LD_LIBRARY_PATH','LD_PRELOAD','LD_AUDIT')}
env['LC_ALL']='C'
version=subprocess.run([str(prefix/'bin/hello'),'--version'],env=env,capture_output=True,text=True,check=True)
greeting=subprocess.run([str(prefix/'bin/hello'),'--greeting=synthetic-prefix-restore'],env=env,capture_output=True,text=True,check=True)
assert version.stdout.splitlines()[0]=='hello (GNU Hello) 2.12.3'
assert greeting.stdout=='synthetic-prefix-restore\n'
trace=subprocess.run([str(prefix/'bin/hello')],env={**env,'LD_DEBUG':'libs'},capture_output=True,text=True,check=True)
assert 'calling init: '+str(prefix/'lib/libc.so.6') in trace.stderr,trace.stderr
result.update(version=version.stdout.splitlines()[0],greeting=greeting.stdout.strip(),bundled_libc_loaded=True)
result['occupied_prefix_refused']=cli('homebrew','restore','--archive',str(archive),'--prefix',str(prefix),'--confirm-manifest-sha256',plan['manifest_sha256'],ok=False)
for name,record in manifest['files'].items(): assert hashlib.sha256((prefix/name).read_bytes()).hexdigest()==record['sha256']
result['after_refusal_bytes_unchanged']=True
Path('/task/target-vertical-receipt.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
