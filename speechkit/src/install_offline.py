# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Verify a supplied archive and install its local wheels without contacting a package index."""
import argparse,hashlib,json,platform,subprocess,sys,zipfile
from pathlib import Path

def sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def install(archive,out,lock):
    policy=json.loads(Path(lock).read_text());archive=Path(archive);out=Path(out)
    if sha(archive)!=policy['archiveSha256']:raise ValueError('Unapproved runtime archive hash')
    if sys.version_info[:2]!=(3,13) or platform.system()!='Linux' or platform.machine() not in ('x86_64','AMD64'):raise ValueError('This supplied wheel bundle targets Linux x86_64 / CPython 3.13 only')
    if out.exists():raise ValueError('Use a new runtime directory')
    with zipfile.ZipFile(archive) as z:
        seen=set();total=0
        for i in z.infolist():
            p=Path(i.filename);total+=i.file_size
            if i.filename in seen or p.is_absolute() or '..' in p.parts or '\\' in i.filename or ((i.external_attr>>16)&0o170000)==0o120000 or i.flag_bits&1:raise ValueError('Unsafe archive member')
            seen.add(i.filename)
        if total>2_000_000_000:raise ValueError('Runtime archive exceeds expansion budget')
        z.extractall(out)
    rows=[]
    for line in (out/'SHA256SUMS').read_text().splitlines():
        h,rel=line.split(None,1);rel=rel.lstrip('*');p=(out/rel).resolve()
        if not p.is_relative_to(out.resolve()) or sha(p)!=h:raise ValueError('Runtime payload identity mismatch')
    for wheel in sorted((out/'wheels').glob('*.whl')):
        with zipfile.ZipFile(wheel) as z:
            metadata=z.read(next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))).decode()
        fields=dict(line.split(': ',1) for line in metadata.splitlines() if line.startswith(('Name: ','Version: ')))
        rows.append(f"{fields['Name']}=={fields['Version']} --hash=sha256:{sha(wheel)}")
    hashed=out/'requirements.hashed.txt';hashed.write_text('\n'.join(rows)+'\n') # The legacy version lock lacks hashes; derive them only after the whole archive is pinned.
    subprocess.run([sys.executable,'-m','venv',str(out/'.venv')],check=True)
    exe=out/'.venv/bin/python'
    subprocess.run([str(exe),'-m','pip','install','--no-index','--find-links',str(out/'wheels'),'--require-hashes','-r',str(hashed)],check=True)
    report={'status':'OFFLINE_RUNTIME_INSTALLED','archiveSha256':sha(archive),'wheelCount':len(rows),'python':str(exe),'inferenceExecuted':False,'networkUsed':False}
    (out/'INSTALL_REPORT.json').write_text(json.dumps(report,indent=2)+'\n');return report
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--archive',required=True);p.add_argument('--out',required=True);p.add_argument('--lock',default=str(Path(__file__).resolve().parents[1]/'runtime-lock.json'));a=p.parse_args()
    try:print(json.dumps(install(a.archive,a.out,a.lock),indent=2))
    except Exception as e:print(json.dumps({'status':'INSTALL_BLOCKED','error':str(e)}),file=sys.stderr);sys.exit(2)
