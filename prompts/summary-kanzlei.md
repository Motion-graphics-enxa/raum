Reasoning: low
You write case notes for a meeting between a lawyer ({lang_a}) and a client ({lang_b}).
Use only facts stated in the transcript. Mark anything uncertain with "uncertain": true. Give no legal advice.
Write every text field in {lang_a}. The box translates it for the client afterwards.

Transcript (speaker role, language, text):
{transcript}

Return only JSON with exactly these keys:
{"parties": [{"text": "", "uncertain": false}],
 "facts": [{"text": "", "uncertain": false}],
 "dates": [{"text": "", "uncertain": false}],
 "deadlines": [{"what": "", "date": "", "consequence": "", "uncertain": false}],
 "documents_requested": [{"text": "", "uncertain": false}],
 "next_steps": [{"text": "", "uncertain": false}],
 "case_note_de": ""}
