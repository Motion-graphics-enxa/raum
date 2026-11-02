import sys, re, json, glob, os
from datetime import datetime
def ts(s):
    h,m,rest = s.split(':'); sec,ms = rest.split('.')
    return int(h)*3600+int(m)*60+int(sec)+int(ms)/1000
LINE = re.compile(r'^(\d\d:\d\d:\d\d\.\d{3}) \+\s*[\d.]+s (\S+)\s+(\S+)\s+lag=\s*-?\d+ms (.*)$')
def parse(path, verbose=False):
    t_speak=dur=None; ev=[]; timings=[]
    for line in open(path, errors='replace'):
        line=line.rstrip('\n')
        m = re.match(r'^SPK (\d\d:\d\d:\d\d\.\d{3}) .*speaking 1/1 \(([\d.]+) s\)', line)
        if m: t_speak=ts(m.group(1)); dur=float(m.group(2)); continue
        if line.startswith('{"utt"'):
            try: timings.append(json.loads(line))
            except Exception: pass
            continue
        m = LINE.match(line)
        if m:
            try: d=json.loads(m.group(4))
            except Exception: d={'_raw': m.group(4)[:200]}
            ev.append((ts(m.group(1)), m.group(2), m.group(3), d))
    if t_speak is None: return None
    stop=t_speak+dur
    r={'run':os.path.basename(path)[:-4],'dur':dur}
    def first(pred):
        for t,u,ty,d in ev:
            if pred(ty,d): return t
    def rel(t, base): return None if t is None else round((t-base)*1000)
    r['p1_from_start']=rel(first(lambda ty,d: ty=='transcript.partial'), t_speak)
    r['delta1_from_start']=rel(first(lambda ty,d: ty=='translation.delta'), t_speak)
    r['voice1_from_start']=rel(first(lambda ty,d: ty=='voice.state' and d.get('playing')), t_speak)
    r['voice1_from_stop']=rel(first(lambda ty,d: ty=='voice.state' and d.get('playing')), stop)
    r['delta1_from_stop']=rel(first(lambda ty,d: ty=='translation.delta'), stop)
    r['uttend_from_stop']=[rel(t,stop) for t,u,ty,d in ev if ty=='utterance.end']
    r['n_utts']=len({u for t,u,ty,d in ev if ty=='utterance.start'})
    r['n_partials']=sum(1 for t,u,ty,d in ev if ty=='transcript.partial')
    segs=[(rel(t,stop), d.get('seg')) for t,u,ty,d in ev if ty=='translation.final' and d.get('seg') is not None]
    r['seg_finals_from_stop']=segs
    r['finals_from_stop']=[rel(t,stop) for t,u,ty,d in ev if ty=='translation.final']
    # partial gaps
    pt=[t for t,u,ty,d in ev if ty=='transcript.partial']
    gaps=[round((b-a)*1000) for a,b in zip(pt,pt[1:])]
    r['partial_gap_first5']=gaps[:5]; r['partial_gap_last5']=gaps[-5:]
    r['partials_after_stop']=sum(1 for t in pt if t>stop)
    r['timings']=timings
    return r
for p in sys.argv[1:]:
    r=parse(p)
    if not r: print(p,'no speak'); continue
    tl=r.pop('timings')
    print(json.dumps(r, ensure_ascii=False))
    for t in tl:
        keep={k:t.get(k) for k in ('utt','target_lang','eot','asr_ms','asr_from','asr_early','mt_first_ms','mt_full_ms','tts_synth_first_ms','tts_publish_first_ms','tts_first_ms','check_ms','simultaneous','segs','voice_clauses','stages','spec')}
        print('   T', json.dumps({k:v for k,v in keep.items() if v is not None}, ensure_ascii=False))
        extra=set(t)-set(keep)-{'models','vad_ms'}
        if extra: print('   extra keys:', sorted(extra))
