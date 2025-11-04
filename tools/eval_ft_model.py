# tools/eval_ft_model.py
# -*- coding: utf-8 -*-
import os, json, random, math

PAIRWISE = os.environ.get("OGIRI_PAIRWISE", "data/processed/pairwise.jsonl")
CANDIDATE_PATH = "models/candidates/last_model.txt"
SAMPLE = int(os.environ.get("OGIRI_EVAL_SAMPLE","100"))  # 評価に使う件数（上限）
SEED = int(os.environ.get("OGIRI_EVAL_SEED","123"))

def ask(client, model, prompt, a, b):
    sys = {"role":"system","content":"あなたは日本語の大喜利審査員です。出力は 'A' または 'B' のみ。"}
    usr = {"role":"user","content":f"お題: {prompt}\nA: {a}\nB: {b}\n面白いのは？"}
    r = client.chat.completions.create(model=model, messages=[sys,usr], temperature=0)
    out = (r.choices[0].message.content or "").strip().upper()
    return "A" if out.startswith("A") else ("B" if out.startswith("B") else out)

def main():
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY",""))

    if not os.path.exists(CANDIDATE_PATH):
        print(json.dumps({"status":"no_candidate"}, ensure_ascii=False)); return
    with open(CANDIDATE_PATH,"r",encoding="utf-8") as f:
        model = f.read().strip()
    if not model:
        print(json.dumps({"status":"empty_candidate"}, ensure_ascii=False)); return

    data=[]
    with open(PAIRWISE,"r",encoding="utf-8") as fr:
        for line in fr:
            line=line.strip()
            if not line: continue
            obj=json.loads(line)
            if not all(k in obj for k in ("prompt_text","winner_text","loser_text")):
                continue
            data.append(obj)
    if not data:
        print(json.dumps({"status":"no_data"}, ensure_ascii=False)); return

    random.seed(SEED)
    random.shuffle(data)
    data=data[:SAMPLE]

    correct=0; total=0
    for ex in data:
        pred = ask(client, model, ex["prompt_text"], ex["winner_text"], ex["loser_text"])
        total += 1
        if pred=="A":  # 教師は常にA=勝者で作っている
            correct += 1

    acc = (correct/total) if total else 0.0
    print(json.dumps({"status":"ok","model":model,"acc":acc,"n":total}, ensure_ascii=False))

if __name__=="__main__":
    main()
