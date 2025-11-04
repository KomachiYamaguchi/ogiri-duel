# tools/ft_openai_train.py
# -*- coding: utf-8 -*-
"""
OpenAI Fine-tuning launcher
----------------------------------
- Uses data/processed/ft_dataset.jsonl
- Creates and monitors fine-tune job
- Saves new model ID to data/models/ft_model_id.txt
"""

import os, json, time, sys
from datetime import datetime

# ========= 設定 =========
DATASET_PATH = os.path.join("data", "processed", "ft_dataset.jsonl")
MODEL_DIR = os.path.join("data", "models")
MODEL_ID_PATH = os.path.join(MODEL_DIR, "ft_model_id.txt")

# ベースモデル（mini or 4o）
BASE_MODEL = os.environ.get("OGIRI_FT_BASE", "gpt-4o-mini")

# SDK 自動検出
def _make_openai_client():
    try:
        from openai import OpenAI
        return ("v1", OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")))
    except Exception:
        try:
            import openai
            openai.api_key = os.environ.get("OPENAI_API_KEY", "")
            return ("legacy", openai)
        except Exception:
            return ("none", None)

OPENAI_SDK, OPENAI_CLIENT = _make_openai_client()

def _ensure_dir(p):
    os.makedirs(p, exist_ok=True)

# ========= 実行関数 =========
def main():
    if not os.path.exists(DATASET_PATH):
        print(f"[ERR] dataset not found: {DATASET_PATH}")
        sys.exit(1)

    if not os.environ.get("OPENAI_API_KEY"):
        print("[ERR] OPENAI_API_KEY not set.")
        sys.exit(1)

    _ensure_dir(MODEL_DIR)

    print(f"[INFO] Using base model: {BASE_MODEL}")
    print(f"[INFO] Dataset: {DATASET_PATH}")

    # ========== Upload ========== #
    if OPENAI_SDK == "v1":
        client = OPENAI_CLIENT
        file_obj = client.files.create(
            file=open(DATASET_PATH, "rb"),
            purpose="fine-tune"
        )
        file_id = file_obj.id
    else:
        client = OPENAI_CLIENT
        file_obj = client.File.create(
            file=open(DATASET_PATH, "rb"),
            purpose="fine-tune"
        )
        file_id = file_obj["id"]

    print(f"[OK] Uploaded dataset file_id={file_id}")

    # ========== Create job ========== #
    if OPENAI_SDK == "v1":
        job = client.fine_tuning.jobs.create(
            training_file=file_id,
            model=BASE_MODEL,
            suffix="ogiri-duel"
        )
        job_id = job.id
    else:
        job = client.FineTuningJob.create(
            training_file=file_id,
            model=BASE_MODEL,
            suffix="ogiri-duel"
        )
        job_id = job["id"]

    print(f"[OK] Fine-tune job started: {job_id}")

    # ========== Monitor ========== #
    print("[INFO] Waiting for job to complete...")
    while True:
        if OPENAI_SDK == "v1":
            j = client.fine_tuning.jobs.retrieve(job_id)
            status = j.status
        else:
            j = client.FineTuningJob.retrieve(job_id)
            status = j["status"]

        print(f"  status={status}")
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(30)

    # ========== 結果 ========== #
    if status == "succeeded":
        if OPENAI_SDK == "v1":
            new_model = j.fine_tuned_model
        else:
            new_model = j["fine_tuned_model"]
        print(f"[OK] Fine-tune succeeded! New model: {new_model}")
        with open(MODEL_ID_PATH, "w", encoding="utf-8") as f:
            f.write(new_model)
        print(f"[SAVED] {MODEL_ID_PATH}")
    else:
        print(f"[FAIL] Fine-tune ended with status={status}")
        sys.exit(2)

    print(json.dumps({
        "status": status,
        "base_model": BASE_MODEL,
        "new_model": new_model if status == "succeeded" else None,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
