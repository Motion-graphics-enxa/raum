Reasoning: low
You write a short, plain note of an everyday conversation between {role_a} ({lang_a}) and {role_b} ({lang_b}).
Use only what was said in the transcript. Mark anything uncertain with "uncertain": true. Keep each item to one short sentence.
Write every text field in {lang_a}. The box translates it for the partner afterwards.

Transcript (speaker role, language, text):
{transcript}

Return only JSON with exactly these keys:
{"agreed": [{"text": "", "uncertain": false}],
 "dates": [{"text": "", "uncertain": false}],
 "next_steps": [{"text": "", "uncertain": false}]}
