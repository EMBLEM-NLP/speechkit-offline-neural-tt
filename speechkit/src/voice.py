# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EMBLEM-NLP
"""Project-independent local neural speech adapter. No formant-waveform fallback."""
from __future__ import annotations
import argparse, hashlib, importlib.metadata, json, math, re, sys, time, wave
from pathlib import Path
from datetime import datetime, timezone

def sha(file):
    h=hashlib.sha256() # Model and speech identities are always explicit.
    with Path(file).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def save(file,data):
    p=Path(file);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(data,indent=2)+'\n');tmp.replace(p) # Atomic resumable checkpoints.

def inside(root,relative):
    p=Path(relative)
    if p.is_absolute() or '..' in p.parts or '\\' in str(relative):raise ValueError('Unsafe model path')
    resolved=(Path(root)/p).resolve(strict=True)
    if not resolved.is_relative_to(Path(root).resolve()):raise ValueError('Model escaped runtime root')
    return resolved

def validate_script(script):
    if script.get('schemaVersion')!='1.0.0' or not isinstance(script.get('segments'),list) or not script['segments']:raise ValueError('Unsupported/empty speech request')
    ids=set()
    for s in script['segments']:
        if not re.fullmatch('[a-z][a-z0-9_-]{0,95}',s.get('id','')) or s['id'] in ids:raise ValueError('Invalid/duplicate segment id')
        ids.add(s['id'])
        if not isinstance(s.get('text'),str) or not s['text'].strip():raise ValueError('Full spoken content required')
        if not isinstance(s.get('spokenText',s['text']),str) or not s.get('spokenText',s['text']).strip():raise ValueError('Empty pronunciation string')
    return script # The adapter does not summarize, translate or rewrite the script.

def doctor(runtime,profile):
    if profile.get('engine')!='piper':raise ValueError('Unsupported engine profile')
    for field,expected in [('piper-tts',profile['engineVersion']),('onnxruntime',profile['onnxruntimeVersion'])]:
        if importlib.metadata.version(field)!=expected:raise ValueError('Incompatible neural dependency '+field)
    for field,digest in [('model','modelSha256'),('config','configSha256')]:
        if sha(inside(runtime,profile[field]))!=profile[digest]:raise ValueError('Pinned voice identity mismatch')
    scale=profile['lengthScale'];pause=profile['sentencePauseSeconds']
    if not math.isfinite(scale) or not .9<=scale<=1.4 or not math.isfinite(pause) or not 0<=pause<=.65:raise ValueError('Unsafe performance parameters')
    return {'status':'PREFLIGHT_PASS','engine':'Piper','engineVersion':profile['engineVersion'],'modelSha256':profile['modelSha256'],'sampleRate':profile['sampleRate'],'networkUsed':False,'waveformFallback':'DISABLED','naturalnessScore':None}

def pcm(file):
    import numpy as np
    with wave.open(str(file),'rb') as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2:raise ValueError('Expected mono PCM16')
        return np.frombuffer(w.readframes(w.getnframes()),dtype='<i2'),w.getframerate()

def synthesize(runtime,profile_path,script_path,out):
    import numpy as np # Loaded only in the explicit neural runtime.
    import onnxruntime as ort
    from piper import PiperVoice,SynthesisConfig
    from piper.config import PiperConfig
    runtime=Path(runtime);out=Path(out);profile=json.loads(Path(profile_path).read_text());script=validate_script(json.loads(Path(script_path).read_text()));preflight=doctor(runtime,profile)
    request={'schemaVersion':'1.0.0','scriptSha256':sha(script_path),'profileSha256':sha(profile_path),'adapterVersion':'1.0.0'}
    if out.exists() and any(out.iterdir()):
        if not (out/'REQUEST.json').exists() or json.loads((out/'REQUEST.json').read_text())!=request:raise ValueError('Stale/unverified voice cache')
    out.mkdir(parents=True,exist_ok=True);save(out/'REQUEST.json',request)
    opts=ort.SessionOptions();opts.intra_op_num_threads=2;opts.inter_op_num_threads=1
    session=ort.InferenceSession(str(inside(runtime,profile['model'])),sess_options=opts,providers=['CPUExecutionProvider'])
    cfg=PiperConfig.from_dict(json.loads(inside(runtime,profile['config']).read_text()))
    if cfg.sample_rate!=profile['sampleRate']:raise ValueError('Native sample rate mismatch')
    rate=cfg.sample_rate;voice=PiperVoice(session=session,config=cfg);rows=[]
    for s in script['segments']:
        filename=s['id']+'.wav';target=out/filename;record=out/(filename+'.json');spoken=s.get('spokenText') or s['text']
        if record.exists():
            row=json.loads(record.read_text())
            if row['request']!=request or row['spokenText']!=spoken or sha(target)!=row['sha256']:raise ValueError('Changed cached speech: '+s['id'])
            rows.append(row);continue
        pieces=re.split(r'(?<=[.!?])\s+',spoken) # Split complete sentences, not each technical word.
        scale=profile['lengthScale'];attempts=[]
        while True:
            buffers=[];raw_count=0
            for i,text in enumerate(pieces):
                if i:buffers.append(np.zeros(round(profile['sentencePauseSeconds']*rate),dtype='<i2'))
                chunks=list(voice.synthesize(text,SynthesisConfig(length_scale=scale,volume=.8)))
                if not chunks:raise ValueError('Neural model returned no waveform')
                for chunk in chunks:
                    samples=np.frombuffer(chunk.audio_int16_bytes,dtype='<i2').copy();buffers.append(samples);raw_count+=len(samples)
            speech=np.concatenate(buffers);duration=len(speech)/rate;wpm=len(spoken.split())*60/duration
            attempts.append({'lengthScale':scale,'durationSeconds':duration,'deliveryWpm':wpm})
            if wpm<=profile['maxDeliveryWpm']:break
            if scale>=1.399:raise ValueError('PACE_REVIEW_REQUIRED: '+s['id'])
            scale=min(1.4,scale+.06) # Adjust the model duration parameter, never stretch a rendered waveform.
        peak=int(np.max(np.abs(speech.astype(np.int32))));rms=float(np.sqrt(np.mean((speech.astype(float)/32768)**2)))
        if not len(speech) or rms<.002 or np.count_nonzero(np.abs(speech.astype(np.int32))>=32760)>len(speech)*.001:raise ValueError('Speech signal gate failed')
        temp=target.with_suffix('.wav.tmp')
        with wave.open(str(temp),'wb') as w:w.setparams((1,2,rate,0,'NONE','not compressed'));w.writeframes(speech.tobytes())
        temp.replace(target)
        row={'id':s['id'],'text':s['text'],'spokenText':spoken,'path':filename,'sha256':sha(target),'sampleRate':rate,'samples':len(speech),'durationSeconds':duration,'deliveryWpm':wpm,'rawChunkWpm':len(spoken.split())*60/(raw_count/rate),'rms':rms,'peak':peak,'request':request,'attempts':attempts,'sourceCoverage':'full submitted string; not ASR verification','pronunciationApproval':'PENDING'}
        save(record,row);rows.append(row);print(s['id']+' synthesized',file=sys.stderr,flush=True)
    result={'schemaVersion':'1.0.0','status':'NEURAL_AUDIO_GENERATED_REVIEW_PENDING','engine':preflight,'segments':rows,'syntheticVoice':True,'generatedAt':datetime.now(timezone.utc).isoformat(),'waveformTimeStretch':False,'pitchShift':False,'naturalnessScore':None,'humanApproval':'PENDING'}
    save(out/'speech.json',result);return result

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['doctor','synthesize']);p.add_argument('--runtime',required=True);p.add_argument('--profile',required=True);p.add_argument('--script');p.add_argument('--out');a=p.parse_args()
    result=doctor(a.runtime,json.loads(Path(a.profile).read_text())) if a.command=='doctor' else synthesize(a.runtime,a.profile,a.script,a.out)
    print(json.dumps(result,indent=2)) # stdout remains structured for the video host.
if __name__=='__main__':
    try:main()
    except Exception as e:print(json.dumps({'status':'EXECUTION_BLOCKED','error':str(e),'waveformFallback':'DISABLED'}),file=sys.stderr);sys.exit(2)
