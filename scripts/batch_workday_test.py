"""Batch-test the hardened Workday filler on 10 real reqs (no submit). Each runs in
COLLAB_NO_WAIT mode: fill → capture outcome → close. Reuses a seeded resume (no LLM
tailoring). Cross-tenant (NVIDIA + Workday Inc.) exercises federated Google SSO + the
auth-resilience / no-SSO detection."""
import json, os, shutil, subprocess, sys, uuid, time
from datetime import datetime, timezone

NV = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job"
WD = "https://workday.wd5.myworkdayjobs.com/en-US/Workday/job"
JOBS = [
    ("NVIDIA", "Senior System SW Engineer, Data Center Diagnostics",
     f"{NV}/US-CA-Santa-Clara/Senior-System-Software-Engineer--Data-Center-Diagnostics_JR2018330"),
    ("NVIDIA", "Senior System SW Engineer, Data Platform Observability",
     f"{NV}/US-CA-Santa-Clara/Senior-System-Software-Engineer---Data-Platform-Observability_JR2012588"),
    ("NVIDIA", "Senior SW QA Engineer, Map Data Validation - AV",
     f"{NV}/US-CA-Santa-Clara/Senior-Software-QA-Engineer--Map-Data-Validation---Autonomous-Vehicles_JR2014043"),
    ("NVIDIA", "Senior Systems SW Engineer, DC Infra Mgmt - EngOps",
     f"{NV}/US-TX-Remote/Senior-Systems-Software-Engineer--Data-Center-Infrastructure-Management---EngOps_JR2015514"),
    ("NVIDIA", "Senior System SW Engineer - DC GPU Compute Diagnostics",
     f"{NV}/US-NC-Durham/Senior-System-Software-Engineer---Data-Center-GPU-Compute-Diagnostics_JR2018218"),
    ("NVIDIA", "Senior Software Architect - Data Center Systems",
     f"{NV}/US-CA-Santa-Clara/Senior-Software-Architect---Data-Center-Systems_JR1973150"),
    ("NVIDIA", "Senior Software Engineer - Datacenter Systems",
     f"{NV}/US-CA-Santa-Clara/Senior-Software-Engineer---Datacenter-Systems_JR2017468"),
    ("NVIDIA", "Senior SW Engineer, Deep Learning - MLIR TRT",
     f"{NV}/US-CA-Santa-Clara/Senior-Software-Engineer--Deep-Learning---MLIR-TRT_JR2009329"),
    ("Workday", "Software Engineer / Senior SW Engineer - AI Platform",
     f"{WD}/USA-CO-Boulder/Software-Engineer-and-Senior-Software-Engineer---AI-Platform_JR-0106467"),
    ("Workday", "Sr Software Engineer, DevOps",
     f"{WD}/USA-VA-McLean/Sr-Software-Engineer--DevOps_JR-0104808"),
]

STATE = "data/pipeline_state.json"
PY = ".venv/bin/python"
RESULTS = "/tmp/batch_results.jsonl"


def inject_and_seed():
    s = json.load(open(STATE))
    ids = []
    for tenant, title, url in JOBS:
        jid = str(uuid.uuid4())
        s["job_listings"].append({
            "id": jid, "company_id": str(uuid.uuid4()), "company_name": tenant,
            "title": title, "description": f"{title} at {tenant}. Python, distributed systems, "
            "data/ML infrastructure, Kubernetes, cloud.", "required_skills": ["Python", "Kubernetes"],
            "nice_to_have_skills": [], "location": "US", "remote": False,
            "salary_min": 160000, "salary_max": 300000, "apply_url": url,
            "source": "workday", "discovered_at": datetime.now(timezone.utc).isoformat()})
        shutil.copy("data/resume_base.md", f"data/resumes/{jid}.md")  # seed cache (skip tailoring)
        ids.append((jid, tenant, title, url))
    json.dump(s, open(STATE, "w"))
    return ids


def run_one(jid):
    subprocess.run("pkill -f 'user-data-dir=/Users/rahul/Workspace/Job_Finder/data/chrome_profile'",
                   shell=True)
    time.sleep(2)
    for lk in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try: os.unlink(f"data/chrome_profile/{lk}")
        except OSError: pass
    env = {**os.environ, "COLLAB_NO_WAIT": "1", "WORKDAY_GOOGLE_LOGIN": "auto",
           "COLLAB_REUSE_RESUME": "1", "COLLAB_FILLER_TIMEOUT": "200"}
    try:
        p = subprocess.run([PY, "scripts/collab_submit.py", "workday", jid],
                           env=env, capture_output=True, text=True, timeout=320)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        out = "<<RESULT>> status=timeout"
    line = next((l for l in reversed(out.splitlines())
                 if any(m in l for m in ("<<RESULT>>", "<<NEEDS_AUTH>>", "<<BLOCKED>>"))), "<<no result>>")
    return line.strip()


def main():
    open(RESULTS, "w").close()
    ids = inject_and_seed()
    print(f"injected {len(ids)} jobs", flush=True)
    for i, (jid, tenant, title, url) in enumerate(ids, 1):
        print(f"[{i}/10] {tenant}: {title[:46]} …", flush=True)
        res = run_one(jid)
        rec = {"n": i, "tenant": tenant, "title": title, "jid": jid, "result": res}
        with open(RESULTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"     -> {res}", flush=True)
    print("BATCH DONE", flush=True)


if __name__ == "__main__":
    main()
