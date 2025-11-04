# -*- coding: utf-8 -*-
import sys, os, json
from datetime import datetime, timezone
from openai import OpenAI

def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/ft_openai_inspect.py <ftjob_id>")
        sys.exit(1)
    job_id = sys.argv[1]
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY",""))
    job = client.fine_tuning.jobs.retrieve(job_id)
    print("[JOB]")
    print(json.dumps(job.model_dump(), ensure_ascii=False, indent=2))

    print("\n[EVENTS]")
    events = client.fine_tuning.jobs.list_events(job_id, limit=50)
    for ev in events.data[::-1]:
        ts = ev.created_at
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00","Z")
        print(f"- {ts} | {ev.level}: {ev.message}")

if __name__ == "__main__":
    main()
