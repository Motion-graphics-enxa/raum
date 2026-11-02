import sys, json, glob, importlib.util
spec=importlib.util.spec_from_file_location('p','parse.py')
src=open('parse.py').read().split('for p in sys.argv[1:]:')[0]
ns={}; exec(src, ns)
rows=[]
for p in sorted(glob.glob('runs/*.log')):
    r=ns['parse'](p)
    if not r: continue
    t=(r['timings'] or [{}])[-1]; st=t.get('stages') or {}
    ap=st.get('asr.partial',{}); a=st.get('asr',{}); sb=st.get('slot.background',{}); sm=st.get('spec.mt',{})
    eot=t.get('eot',{})
    print(' | '.join(str(x) for x in [r['run'], r['dur'], r['p1_from_start'], r['delta1_from_start'], r['voice1_from_start'],
        len(r['seg_finals_from_stop']) or 0, r['uttend_from_stop'], r['voice1_from_stop'],
        f"{eot.get('why')}/{eot.get('wait_ms')}" if eot else '-', t.get('asr_ms'), t.get('asr_from'), t.get('mt_first_ms'), t.get('tts_publish_first_ms'),
        f"{int(ap.get('n',0))}x {ap.get('wall_ms')}ms/{ap.get('audio_ms')}", f"{a.get('wall_ms')}/{a.get('audio_ms')}",
        f"bg wait {sb.get('wait_ms')}", f"spec.mt n{int(sm.get('n',0))} {sm.get('wall_ms')}ms", t.get('simultaneous'), t.get('segs'), r['partial_gap_last5'], r['n_utts']]))
