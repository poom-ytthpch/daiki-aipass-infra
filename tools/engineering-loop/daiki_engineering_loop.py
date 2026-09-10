#!/usr/bin/env python3
"""Daiki AIPass repeatable engineering/evaluation loop.

No third-party Python packages are required.  The API suite talks to the
cluster-internal backend via a temporary kubectl port-forward, so simulated
Guest IPs never need to be trusted through the public edge.  Hermes capability
checks use profile-scoped one-shot runs inside the existing Hermes pod and do
not create application users or API keys.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import http.client
import json
import os
import pathlib
import random
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Iterable

ROOT = pathlib.Path(__file__).resolve().parent
VISION_FIXTURE = ROOT / "fixtures" / "vision-marker.png"
DOC_FIXTURE = ROOT / "fixtures" / "document-marker.txt"
VISION_MARKER = "DAIKI-VISION-4827"
DOC_MARKER = "DOCUMENT-ALPHA-7319"

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
SKIP = "SKIP"

@dataclasses.dataclass
class Result:
    name: str
    group: str
    status: str
    latency_ms: float = 0.0
    http_status: int | None = None
    detail: str = ""
    metrics: dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

class Report:
    def __init__(self, target: str) -> None:
        self.target = target
        self.started = time.time()
        self.results: list[Result] = []
        self._lock = threading.Lock()

    def add(self, result: Result) -> Result:
        with self._lock:
            self.results.append(result)
        icon = {PASS:"✓",FAIL:"✗",BLOCKED:"!",SKIP:"-"}.get(result.status,"?")
        suffix = f" {result.latency_ms:.0f}ms" if result.latency_ms else ""
        print(f"{icon} [{result.status:<7}] {result.group}/{result.name}{suffix} {result.detail}", flush=True)
        return result

    def summary(self) -> dict[str, Any]:
        counts = {s: sum(r.status == s for r in self.results) for s in (PASS,FAIL,BLOCKED,SKIP)}
        overall = FAIL if counts[FAIL] else ("DEGRADED" if counts[BLOCKED] else PASS)
        return {"overall":overall,"counts":counts,"durationSeconds":round(time.time()-self.started,2)}

    def write(self, out_dir: pathlib.Path) -> tuple[pathlib.Path,pathlib.Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp=time.strftime("%Y%m%d-%H%M%S",time.gmtime())
        payload={"target":self.target,"startedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(self.started)),"summary":self.summary(),"results":[r.as_dict() for r in self.results]}
        jp=out_dir/f"engineering-loop-{stamp}.json"; jp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n")
        lines=["# Daiki Engineering Loop", "", f"- Target: `{self.target}`", f"- Overall: **{payload['summary']['overall']}**", f"- Duration: {payload['summary']['durationSeconds']}s", "", "| Group | Scenario | Status | Latency | Detail |", "|---|---|---:|---:|---|"]
        for r in self.results:
            detail=r.detail.replace("|","\\|").replace("\n"," ")[:180]
            lines.append(f"| {r.group} | {r.name} | {r.status} | {r.latency_ms:.0f} ms | {detail} |")
        lines += ["", "## Summary", "", "```json", json.dumps(payload["summary"],ensure_ascii=False,indent=2), "```", ""]
        mp=out_dir/f"engineering-loop-{stamp}.md"; mp.write_text("\n".join(lines))
        return jp,mp

@dataclasses.dataclass
class HTTPResult:
    status: int
    headers: dict[str,str]
    body: bytes
    latency_ms: float

class API:
    def __init__(self, base: str, timeout: float = 120.0):
        self.base=base.rstrip("/"); self.timeout=timeout

    def request(self, method: str, path: str, *, headers: dict[str,str]|None=None, data: bytes|None=None, json_body: Any=None) -> HTTPResult:
        h=dict(headers or {})
        if json_body is not None:
            data=json.dumps(json_body,separators=(",",":"),ensure_ascii=False).encode()
            h.setdefault("Content-Type","application/json")
        req=urllib.request.Request(self.base+path,data=data,method=method,headers=h)
        start=time.perf_counter()
        try:
            with urllib.request.urlopen(req,timeout=self.timeout) as resp:
                body=resp.read(); status=resp.status; rh={k.lower():v for k,v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            body=e.read(); status=e.code; rh={k.lower():v for k,v in e.headers.items()}
        return HTTPResult(status,rh,body,(time.perf_counter()-start)*1000)

    def multipart(self,path:str,file_path:pathlib.Path,headers:dict[str,str],source:str="file") -> HTTPResult:
        boundary="----daiki-loop-"+uuid.uuid4().hex
        blob=file_path.read_bytes(); mime="image/png" if file_path.suffix.lower()==".png" else "text/plain"
        parts=[]
        def field(name:str,value:str):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        field("source",source); field("relativePath",file_path.name)
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_path.name}\"\r\nContent-Type: {mime}\r\n\r\n".encode()+blob+b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("POST",path,headers={**headers,"Content-Type":f"multipart/form-data; boundary={boundary}"},data=b"".join(parts))

def json_obj(body:bytes) -> Any:
    try: return json.loads(body.decode("utf-8","replace"))
    except Exception: return None

def chat_text(body:bytes) -> str:
    d=json_obj(body)
    if not isinstance(d,dict): return ""
    try: c=d["choices"][0]["message"]["content"]
    except Exception: return ""
    if isinstance(c,str): return c
    if isinstance(c,list): return "".join(x.get("text","") for x in c if isinstance(x,dict))
    return ""

def sse_text(body:bytes) -> str:
    out=[]
    for raw in body.decode("utf-8","replace").splitlines():
        line=raw.strip()
        if not line.startswith("data:"): continue
        data=line[5:].strip()
        if not data or data=="[DONE]": continue
        try: d=json.loads(data)
        except Exception: continue
        if isinstance(d,dict) and d.get("error"): raise RuntimeError(str(d["error"]))
        try: chunk=d["choices"][0]["delta"].get("content","")
        except Exception: chunk=""
        if isinstance(chunk,str): out.append(chunk)
    return "".join(out)

def guest_headers(seed:int,name:str="Daiki Loop") -> dict[str,str]:
    # RFC 2544 benchmarking range; used only as an internal identity token.
    third=(seed//250)%250; fourth=(seed%250)+1
    return {
        "X-Daiki-Client-IP":f"198.18.{third}.{fourth}",
        "X-Daiki-Guest-Device-ID":f"engineering-loop-{seed:05d}",
        "X-Daiki-Guest-Device-Name":name,
        "User-Agent":"DaikiEngineeringLoop/1.0",
    }

def header_metrics(h:dict[str,str]) -> dict[str,Any]:
    keys=["x-daiki-model-alias","x-daiki-model-physical","x-daiki-inference-upstream","x-daiki-fallback-model","x-daiki-retry-attempts","x-daiki-context-trimmed","x-daiki-hermes-profile","x-daiki-workload","x-daiki-queue-wait-ms","x-daiki-admission-wait-ms","x-daiki-admission-tokens"]
    return {k:h.get(k,"") for k in keys if h.get(k) is not None}

def add_http(report:Report,name:str,group:str,r:HTTPResult,ok:Callable[[HTTPResult],bool],detail:str="") -> Result:
    status=PASS if ok(r) else FAIL
    body=r.body.decode("utf-8","replace")[:220].replace("\n"," ")
    return report.add(Result(name,group,status,r.latency_ms,r.status,detail or body,header_metrics(r.headers)))

def cleanup(api:API,attachment_id:str|None,headers:dict[str,str]) -> None:
    if not attachment_id: return
    with contextlib.suppress(Exception): api.request("DELETE",f"/guest/attachments/{urllib.parse.quote(attachment_id)}",headers=headers)

def run_api_suite(api:API,report:Report,seed_base:int=1000) -> None:
    add_http(report,"live","api",api.request("GET","/health/live"),lambda r:r.status==200)
    add_http(report,"ready","api",api.request("GET","/health/ready"),lambda r:r.status==200)
    policy=api.request("GET","/guest/policy"); p=json_obj(policy.body) or {}
    report.add(Result("guest-policy","api",PASS if policy.status==200 and p.get("enabled") else FAIL,policy.latency_ms,policy.status,f"req/h={p.get('requestsPerHour')} gap={p.get('minIntervalSeconds')}s upload={p.get('allowUploads')} imageGen={p.get('allowImageGeneration')}",p if isinstance(p,dict) else {}))

    # Exact text / fallback continuity.
    h=guest_headers(seed_base+1)
    r=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","messages":[{"role":"user","content":"Reply exactly DAIKI_LOOP_OK"}],"stream":False})
    txt=chat_text(r.body).strip()
    report.add(Result("text-exact","guest",PASS if r.status==200 and txt=="DAIKI_LOOP_OK" else FAIL,r.latency_ms,r.status,txt[:160],header_metrics(r.headers)))

    # Context continuity in one frontend-equivalent payload.
    h=guest_headers(seed_base+2)
    msgs=[{"role":"user","content":"The project codename is ORCHID-731. Remember it for the next message."},{"role":"assistant","content":"Understood."},{"role":"user","content":"What was the project codename? Reply with the codename only."}]
    r=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","messages":msgs,"stream":False}); txt=chat_text(r.body)
    report.add(Result("context-followup","quality",PASS if r.status==200 and "ORCHID-731" in txt else FAIL,r.latency_ms,r.status,txt[:180],header_metrics(r.headers)))

    # Streaming delivery.
    h=guest_headers(seed_base+3)
    r=api.request("POST","/guest/chat/stream",headers=h,json_body={"model":"fast","messages":[{"role":"user","content":"Reply exactly STREAM_LOOP_OK"}],"stream":True})
    try: txt=sse_text(r.body).strip(); ok=r.status==200 and txt=="STREAM_LOOP_OK"
    except Exception as e: txt=str(e); ok=False
    report.add(Result("stream","guest",PASS if ok else FAIL,r.latency_ms,r.status,txt[:180],header_metrics(r.headers)))

    # Invalid attachment must not consume cooldown.
    h=guest_headers(seed_base+4)
    bad=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","attachmentIds":["att_missing_engineering_loop"],"messages":[{"role":"user","content":"review"}],"stream":False})
    good=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","messages":[{"role":"user","content":"Reply exactly COOLDOWN_NOT_CONSUMED"}],"stream":False})
    txt=chat_text(good.body)
    report.add(Result("invalid-does-not-consume-cooldown","guest",PASS if bad.status==400 and good.status==200 and "COOLDOWN_NOT_CONSUMED" in txt else FAIL,bad.latency_ms+good.latency_ms,good.status,f"invalid={bad.status} next={good.status} {txt[:100]}",header_metrics(good.headers)))

    # Text-file upload -> list -> grounded chat -> download -> cleanup.
    h=guest_headers(seed_base+10,"Loop Document")
    up=api.multipart("/guest/attachments",DOC_FIXTURE,h,"file"); ud=json_obj(up.body) or {}; aid=ud.get("id") if isinstance(ud,dict) else None
    report.add(Result("file-upload","file",PASS if up.status==201 and aid else FAIL,up.latency_ms,up.status,str(aid or up.body[:100]),{}))
    if aid:
        listed=api.request("GET","/guest/attachments",headers=h); ld=json_obj(listed.body)
        report.add(Result("file-list","file",PASS if listed.status==200 and isinstance(ld,list) and any(x.get("id")==aid for x in ld if isinstance(x,dict)) else FAIL,listed.latency_ms,listed.status,f"count={len(ld) if isinstance(ld,list) else '?'}",{}))
        chat=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","attachmentIds":[aid],"messages":[{"role":"user","content":"Read the attached file and return only its exact verification marker."}],"stream":False}); txt=chat_text(chat.body)
        report.add(Result("file-grounding","quality",PASS if chat.status==200 and DOC_MARKER in txt else FAIL,chat.latency_ms,chat.status,txt[:180],header_metrics(chat.headers)))
        down=api.request("GET",f"/guest/attachments/{aid}",headers=h)
        report.add(Result("file-download","file",PASS if down.status==200 and DOC_MARKER.encode() in down.body else FAIL,down.latency_ms,down.status,f"bytes={len(down.body)}",{}))
        cleanup(api,aid,h)

    # Native image understanding. Marker exists only in pixels.
    h=guest_headers(seed_base+20,"Loop Vision")
    up=api.multipart("/guest/attachments",VISION_FIXTURE,h,"image"); ud=json_obj(up.body) or {}; aid=ud.get("id") if isinstance(ud,dict) else None
    report.add(Result("image-upload","image",PASS if up.status==201 and aid else FAIL,up.latency_ms,up.status,str(aid or up.body[:100]),{}))
    if aid:
        chat=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","attachmentIds":[aid],"messages":[{"role":"user","content":"Read the exact large alphanumeric code visible in the attached image. Return only the code."}],"stream":False}); txt=chat_text(chat.body).strip()
        report.add(Result("native-vision","quality",PASS if chat.status==200 and txt==VISION_MARKER else FAIL,chat.latency_ms,chat.status,txt[:180],header_metrics(chat.headers)))
        cleanup(api,aid,h)

    # File generation is a real Guest capability and must produce a downloadable artifact.
    h=guest_headers(seed_base+30,"Loop FileGen")
    fg=api.request("POST","/guest/generate/file",headers=h,json_body={"prompt":"Create a plain text file containing exactly FILE-GEN-LOOP-8821 and nothing else.","name":"loop-generated.txt","mediaType":"text/plain"})
    fd=json_obj(fg.body) or {}; att=fd.get("attachment",{}) if isinstance(fd,dict) else {}; aid=att.get("id") if isinstance(att,dict) else None
    status=PASS if fg.status==201 and aid else FAIL
    report.add(Result("file-generation","generation",status,fg.latency_ms,fg.status,str(fd)[:200],{}))
    if aid:
        down=api.request("GET",f"/guest/attachments/{aid}",headers=h); content=down.body.decode("utf-8","replace")
        report.add(Result("generated-file-content","generation",PASS if down.status==200 and "FILE-GEN-LOOP-8821" in content else FAIL,down.latency_ms,down.status,content[:160],{})); cleanup(api,aid,h)

    # Image generation remains a hard acceptance capability.  Report missing
    # provider credentials as BLOCKED instead of pretending the feature passed.
    h=guest_headers(seed_base+40,"Loop ImageGen")
    ig=api.request("POST","/guest/generate/image",headers=h,json_body={"prompt":"A minimal orange circle centered on a white background","name":"loop-generated.png"})
    idata=json_obj(ig.body) or {}; iatt=idata.get("attachment",{}) if isinstance(idata,dict) else {}; iaid=iatt.get("id") if isinstance(iatt,dict) else None
    if ig.status==201 and iaid:
        report.add(Result("image-generation","generation",PASS,ig.latency_ms,ig.status,str(iaid),{})); cleanup(api,iaid,h)
    elif ig.status in (502,503) and isinstance(idata,dict) and idata.get("error")=="image_generation_unavailable":
        report.add(Result("image-generation","generation",BLOCKED,ig.latency_ms,ig.status,"No active image-generation provider credential",{}))
    else:
        report.add(Result("image-generation","generation",FAIL,ig.latency_ms,ig.status,str(idata)[:200],{}))

def percentile(values:list[float], p:float)->float:
    if not values: return 0.0
    xs=sorted(values); idx=min(len(xs)-1,max(0,int(round((len(xs)-1)*p))))
    return xs[idx]

def run_load(api:API,report:Report,levels:list[int],seed_base:int=20000) -> None:
    # Cheap endpoint load first: exercises ingress/service/backend/json path without model cost.
    def policy_call(i:int)->HTTPResult: return api.request("GET","/guest/policy")
    start=time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex: rows=list(ex.map(policy_call,range(100)))
    lats=[r.latency_ms for r in rows]; ok=sum(r.status==200 for r in rows)
    report.add(Result("policy-100x-c20","load",PASS if ok==100 else FAIL,(time.perf_counter()-start)*1000,None,f"ok={ok}/100 p50={percentile(lats,.50):.0f}ms p95={percentile(lats,.95):.0f}ms",{"requests":100,"concurrency":20,"p50Ms":percentile(lats,.50),"p95Ms":percentile(lats,.95),"maxMs":max(lats or [0])}))

    for level in levels:
        def one(i:int)->tuple[HTTPResult,str]:
            marker=f"LOAD-{level}-{i}-OK"; h=guest_headers(seed_base+level*100+i,f"Load {level}/{i}")
            r=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","messages":[{"role":"user","content":f"Reply exactly {marker}"}],"stream":False})
            return r,marker
        start=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=level) as ex: rows=list(ex.map(one,range(level)))
        elapsed=(time.perf_counter()-start)*1000; lats=[r.latency_ms for r,_ in rows]
        passed=0; statuses={}; models={}; fallbacks=0; admission=[]; admission_tokens=[]
        for r,marker in rows:
            statuses[r.status]=statuses.get(r.status,0)+1
            model=r.headers.get("x-daiki-model-physical",""); models[model]=models.get(model,0)+1
            if r.headers.get("x-daiki-fallback-model"): fallbacks+=1
            with contextlib.suppress(Exception): admission.append(float(r.headers.get("x-daiki-admission-wait-ms","0") or 0))
            with contextlib.suppress(Exception): admission_tokens.append(int(r.headers.get("x-daiki-admission-tokens","0") or 0))
            if r.status==200 and marker in chat_text(r.body): passed+=1
        error_rate=(level-passed)/max(1,level); adm_p95=percentile(admission,.95)
        report.add(Result(f"inference-c{level}","load",PASS if error_rate<=0.10 else FAIL,elapsed,None,f"ok={passed}/{level} error={error_rate:.0%} p50={percentile(lats,.50):.0f}ms p95={percentile(lats,.95):.0f}ms admission-p95={adm_p95:.0f}ms",{"requests":level,"concurrency":level,"passed":passed,"errorRate":error_rate,"p50Ms":percentile(lats,.50),"p95Ms":percentile(lats,.95),"maxMs":max(lats or [0]),"statuses":statuses,"models":models,"fallbacks":fallbacks,"admissionP50Ms":percentile(admission,.50),"admissionP95Ms":adm_p95,"admissionTokensMax":max(admission_tokens or [0])}))
        if error_rate>0.10:
            report.add(Result("ramp-stop","load",SKIP,0,None,f"Stopped after c{level}; error rate exceeded 10%",{})); break

def run_cmd(cmd:list[str],timeout:float=180.0,env:dict[str,str]|None=None)->tuple[int,str,float]:
    started=time.perf_counter(); p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=timeout,env=env); return p.returncode,p.stdout,(time.perf_counter()-started)*1000

def ready_hermes_pod(context:str,namespace:str)->str:
    q="{range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name}{'\\n'}{end}"
    p=subprocess.run(["kubectl","--context",context,"-n",namespace,"get","pod","-l","app=hermes","-o",f"jsonpath={q}"],capture_output=True,text=True,check=True)
    rows=[x.strip() for x in p.stdout.splitlines() if x.strip()]
    if not rows: raise RuntimeError("no ready Hermes pod")
    return rows[-1]

def kubectl_exec(context:str,namespace:str,pod:str,shell_script:str,timeout:float=240.0)->tuple[int,str,float]:
    return run_cmd(["kubectl","--context",context,"-n",namespace,"exec",pod,"--","sh","-lc",shell_script],timeout=timeout)

def run_hermes_suite(context:str,namespace:str,report:Report)->None:
    pod=ready_hermes_pod(context,namespace)
    budgets={
        "user":{"maxTools":2,"maxToolBytes":3000,"maxSkillsBytes":200},
        "skills":{"maxTools":3,"maxToolBytes":4200,"maxSkillsBytes":2200},
        "agent":{"maxTools":1,"maxToolBytes":5000,"maxSkillsBytes":200},
        "research":{"maxTools":0,"maxToolBytes":64,"maxSkillsBytes":200},
        "guest":{"maxTools":0,"maxToolBytes":64,"maxSkillsBytes":200},
    }
    for profile,budget in budgets.items():
        home=f"/opt/data/profiles/{profile}"
        script=f'HERMES_SESSION_PLATFORM=api_server HERMES_HOME={home} /opt/hermes/.venv/bin/hermes prompt-size --platform api_server --json 2>/dev/null'
        code,out,ms=kubectl_exec(context,namespace,pod,script,90)
        try:
            d=json.loads(out[out.find("{"):])
            metrics={"systemBytes":d["system_prompt"]["bytes"],"skillsIndexBytes":d["skills_index"]["bytes"],"toolCount":d["tools"]["count"],"toolBytes":d["tools"]["json_bytes"]}
            ok=code==0 and metrics["toolCount"]<=budget["maxTools"] and metrics["toolBytes"]<=budget["maxToolBytes"] and metrics["skillsIndexBytes"]<=budget["maxSkillsBytes"]
            detail=f"system={metrics['systemBytes']}B skills={metrics['skillsIndexBytes']}B tools={metrics['toolCount']}/{metrics['toolBytes']}B"
        except Exception as e:
            metrics={}; ok=False; detail=f"parse failed: {e}; {out[-200:]}"
        report.add(Result(f"prompt-size-{profile}","hermes",PASS if ok else FAIL,ms,None,detail,metrics))
    script='printf "files="; find /opt/data/profiles/skills/skills -name SKILL.md | wc -l; printf "journey="; HERMES_HOME=/opt/data/profiles/skills /opt/hermes/.venv/bin/hermes journey --json 2>/dev/null | /opt/hermes/.venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(len(d.get(\"nodes\",[])))"'
    code,out,ms=kubectl_exec(context,namespace,pod,script,90)
    m=re.search(r"files=\s*(\d+).*journey=\s*(\d+)",out,re.S)
    metrics={"skillFiles":int(m.group(1)) if m else 0,"journeyNodes":int(m.group(2)) if m else 0}
    report.add(Result("skill-inventory","learning",PASS if code==0 and metrics["skillFiles"]>=50 else FAIL,ms,None,f"files={metrics['skillFiles']} journey={metrics['journeyNodes']}",metrics))
    cases=[
        ("user","plain-intelligence","Reply exactly HERMES_USER_OK","HERMES_USER_OK",None),
        ("skills","skill-view","Use skill_view to inspect the installed skill named codebase-inspection, then answer with SKILL_VIEW_OK followed by one short principle from that skill.","SKILL_VIEW_OK","skills"),
        ("agent","delegation","Use delegate_task exactly once for the tiny task 'What is 6 times 7?'. After it returns, answer exactly AGENT_DELEGATION_OK 42.","AGENT_DELEGATION_OK","delegation"),
    ]
    for profile,name,prompt,needle,toolsets in cases:
        token=uuid.uuid4().hex[:12]; usage=f"/tmp/daiki-loop-{token}.json"; home=f"/opt/data/profiles/{profile}"
        toolarg=f" -t {toolsets}" if toolsets else ""
        platform="HERMES_SESSION_PLATFORM=api_server " if profile=="skills" else ""
        qprompt=json.dumps(prompt)
        script=f"set -e; {platform}HERMES_HOME={home} /opt/hermes/.venv/bin/hermes -z {qprompt}{toolarg} --usage-file {usage}; echo __USAGE__; cat {usage} 2>/dev/null || true"
        code,out,ms=kubectl_exec(context,namespace,pod,script,240)
        answer=out.split("__USAGE__",1)[0].strip(); usage_obj={}
        if "__USAGE__" in out:
            with contextlib.suppress(Exception): usage_obj=json.loads(out.split("__USAGE__",1)[1].strip())
        report.add(Result(name,"hermes",PASS if code==0 and needle in answer else FAIL,ms,None,answer[-240:],usage_obj if isinstance(usage_obj,dict) else {}))
    script='HERMES_HOME=/opt/data/profiles/skills /opt/hermes/.venv/bin/hermes curator run --dry-run --sync 2>&1'
    code,out,ms=kubectl_exec(context,namespace,pod,script,180)
    report.add(Result("curator-dry-run","learning",PASS if code==0 else FAIL,ms,None,out[-300:].replace("\n"," "),{}))

def add_api_coverage(report:Report)->None:
    covered=["GET /health/live","GET /health/ready","GET /guest/policy","POST /guest/chat","POST /guest/chat/stream","GET /guest/attachments","POST /guest/attachments","GET /guest/attachments/{id}","DELETE /guest/attachments/{id}","POST /guest/generate/file","POST /guest/generate/image"]
    isolated=["GET /models","GET /usage","POST /chat","POST /chat/stream","chat-sessions CRUD/runs","authenticated attachments CRUD","quota reset/use","admin summary/queues/audit","admin providers/models/aliases CRUD","admin users/roles/status/quota CRUD","admin API keys/quota CRUD","admin token/guest policy mutation","Gmail OAuth connect/test/disconnect"]
    blocked=["guest image generation: external image provider credential"]
    report.add(Result("safe-prod","coverage",PASS,0,None,f"{len(covered)} route contracts covered",{"routes":covered}))
    report.add(Result("isolated-auth","coverage",BLOCKED,0,None,f"{len(isolated)} authenticated/admin mutation groups require isolated test identity/environment",{"routes":isolated}))
    report.add(Result("external-provider","coverage",BLOCKED,0,None,blocked[0],{"routes":blocked}))

def free_port()->int:
    with socket.socket() as s: s.bind(("127.0.0.1",0)); return s.getsockname()[1]

@contextlib.contextmanager
def backend_port_forward(context:str,namespace:str):
    port=free_port(); cmd=["kubectl","--context",context,"-n",namespace,"port-forward","service/backend",f"{port}:8080"]
    p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        deadline=time.time()+20
        while time.time()<deadline:
            if p.poll() is not None: raise RuntimeError("backend port-forward exited")
            try:
                with socket.create_connection(("127.0.0.1",port),timeout=.2): break
            except OSError: time.sleep(.15)
        else: raise RuntimeError("backend port-forward not ready")
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        p.terminate()
        with contextlib.suppress(Exception): p.wait(timeout=3)
        if p.poll() is None: p.kill()

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--context",default="match-infra")
    ap.add_argument("--namespace",default="daiki-ai-passport")
    ap.add_argument("--suite",choices=["api","hermes","all"],default="all")
    ap.add_argument("--load-levels",default="1,2,5,10")
    ap.add_argument("--skip-load",action="store_true")
    ap.add_argument("--out",default=str(ROOT/"reports"))
    args=ap.parse_args()
    report=Report(f"{args.context}/{args.namespace}")
    if args.suite in ("api","all"):
        with backend_port_forward(args.context,args.namespace) as base:
            api=API(base)
            run_api_suite(api,report)
            if not args.skip_load:
                levels=[int(x) for x in args.load_levels.split(",") if x.strip()]
                run_load(api,report,levels)
    if args.suite in ("hermes","all"):
        run_hermes_suite(args.context,args.namespace,report)
    add_api_coverage(report)
    jp,mp=report.write(pathlib.Path(args.out))
    print(f"\nReport JSON: {jp}\nReport Markdown: {mp}\nSummary: {json.dumps(report.summary(),ensure_ascii=False)}")
    return 1 if report.summary()["counts"][FAIL] else 0

if __name__=="__main__": raise SystemExit(main())
