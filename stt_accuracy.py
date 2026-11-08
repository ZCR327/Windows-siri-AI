# -*- coding: utf-8 -*-
import os, subprocess, sys, difflib, json
TMP = os.path.dirname(os.path.abspath(__file__))
WS = TMP
PS1 = os.path.join(TMP, 'stt_wav.ps1')

CASES = [
    ("zh-CN", "zh", "今天天气怎么样"),
    ("zh-CN", "zh", "请帮我把这份报告的第三章总结成三点"),
    ("zh-CN", "zh", "微信支付到账五十元谢谢"),
    ("zh-CN", "zh", "明天早上八点提醒我出门"),
    ("zh-CN", "zh", "这台电脑的电池还剩多少电"),
    ("en-US", "en", "the quick brown fox jumps over the lazy dog"),
    ("en-US", "en", "remind me to call the dentist tomorrow at ten"),
    ("en-US", "en", "set a reminder for three thirty in the afternoon"),
    ("en-US", "en", "open the settings panel and switch to the English model"),
    ("en-US", "en", "what is the current market breadth percentage"),
]

import win32com.client
voice = win32com.client.Dispatch("SAPI.SpVoice")
toks = [(t.GetDescription(), t) for t in voice.GetVoices()]
def pick(want):
    for d, t in toks:
        low = d.lower()
        if want == "zh" and ("huihui" in low or "chinese" in low): return t
        if want == "en" and ("zira" in low or "english" in low): return t
    return None

def make_wav(path, text, tok):
    st = win32com.client.Dispatch("SAPI.SpFileStream")
    st.Format.Type = 18          # SAFT16kHz16BitMono（识别器原生采样率）
    st.Open(path, 3)
    voice.AudioOutputStream = st
    voice.Voice = tok
    voice.Rate = 0
    voice.Speak(text, 0)
    st.Close()

def recognize(wav, lang):
    out = os.path.join(TMP, 'stt_wav_out.txt')
    if os.path.exists(out): os.remove(out)
    subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",PS1,
                    "-WavFile",wav,"-Language",lang,"-OutFile",out],
                   capture_output=True, text=True)
    try:
        with open(out, 'r', encoding='utf-8') as f: return f.read()
    except Exception: return ""

rows = []
for i,(lang,vh,text) in enumerate(CASES):
    wav = os.path.join(TMP, 'acc16_%d.wav' % i)
    make_wav(wav, text, pick(vh))
    got = recognize(wav, lang).strip()
    e = text.strip(); g = got
    ca = difflib.SequenceMatcher(None, e, g).ratio() if g else 0.0
    ew, gw = e.split(), g.split()
    wa = difflib.SequenceMatcher(None, ew, gw).ratio() if g else 0.0
    rows.append(dict(lang=lang, expected=e, heard=g, char_acc=round(ca,4), word_acc=round(wa,4)))
    print('[%s] 字准 %5.1f%%  词准 %5.1f%%' % (lang, ca*100, wa*100))
    print('   说: %s' % e)
    print('   听: %s' % (g if g else '(空)'))
    print()

for lang in ('zh-CN','en-US'):
    sub=[r for r in rows if r['lang']==lang]
    c=sum(r['char_acc'] for r in sub)/len(sub); w=sum(r['word_acc'] for r in sub)/len(sub)
    print('%s 平均：字准 %.1f%%  词准 %.1f%%  (%d 句)' % (lang, c*100, w*100, len(sub)))
with open(os.path.join(TMP,'stt_accuracy.json'),'w',encoding='utf-8') as f:
    json.dump(rows,f,ensure_ascii=False,indent=2)
