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
import shlex
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
import zipfile
from typing import Any, Callable, Iterable

ROOT = pathlib.Path(__file__).resolve().parent
VISION_FIXTURE = ROOT / "fixtures" / "vision-marker.png"
DOC_FIXTURE = ROOT / "fixtures" / "document-marker.txt"
VISION_MARKER = "DAIKI-VISION-4827"
DOC_MARKER = "DOCUMENT-ALPHA-7319"
PDF_MARKER = "PDF-ALPHA-8421"
XLSX_MARKER = "XLSX-OMEGA-5521"
CSV_SKU = "CSV-TARGET-7319"
CSV_STATUS = "CANCELLED-7319"

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
SKIP = "SKIP"

def build_pdf_fixture(path:pathlib.Path, marker:str=PDF_MARKER) -> None:
    stream=f"BT /F1 18 Tf 72 720 Td ({marker}) Tj ET".encode("ascii")
    objects=[
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream",
    ]
    out=bytearray(b"%PDF-1.4\n")
    offsets=[0]
    for idx,obj in enumerate(objects,1):
        offsets.append(len(out)); out.extend(f"{idx} 0 obj\n".encode()); out.extend(obj); out.extend(b"\nendobj\n")
    xref=len(out); out.extend(f"xref\n0 {len(objects)+1}\n".encode()); out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]: out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(out)

def build_xlsx_fixture(path:pathlib.Path, marker:str=XLSX_MARKER) -> None:
    content_types='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'''
    rels='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    wb_rels='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'''
    sheet=f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>key</t></is></c><c r="B1" t="inlineStr"><is><t>value</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>verification</t></is></c><c r="B2" t="inlineStr"><is><t>{marker}</t></is></c></row></sheetData></worksheet>'''
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",content_types)
        z.writestr("_rels/.rels",rels)
        z.writestr("xl/workbook.xml",workbook)
        z.writestr("xl/_rels/workbook.xml.rels",wb_rels)
        z.writestr("xl/worksheets/sheet1.xml",sheet)

def build_large_csv_fixture(path:pathlib.Path) -> None:
    with path.open("w",encoding="utf-8",newline="") as f:
        f.write("sku,price,status,note\n")
        for i in range(24000):
            if i==19000:
                f.write(f"{CSV_SKU},99,{CSV_STATUS},deep-target\n")
            else:
                f.write(f"SKU-{i:05d},{i%97},OK,row-{i}\n")

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
        blob=file_path.read_bytes(); ext=file_path.suffix.lower(); mime={".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp",".pdf":"application/pdf",".xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",".xlsm":"application/vnd.ms-excel.sheet.macroEnabled.12",".csv":"text/csv",".tsv":"text/tab-separated-values"}.get(ext,"text/plain")
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
    keys=["x-daiki-model-alias","x-daiki-model-physical","x-daiki-inference-upstream","x-daiki-fallback-model","x-daiki-retry-attempts","x-daiki-context-trimmed","x-daiki-hermes-profile","x-daiki-workload","x-daiki-response-language","x-daiki-command-skills","x-daiki-auto-skills","x-daiki-queue-wait-ms","x-daiki-admission-wait-ms","x-daiki-admission-tokens","x-daiki-admission-spillover"]
    return {k:h.get(k,"") for k in keys if h.get(k) is not None}

def add_http(report:Report,name:str,group:str,r:HTTPResult,ok:Callable[[HTTPResult],bool],detail:str="") -> Result:
    status=PASS if ok(r) else FAIL
    body=r.body.decode("utf-8","replace")[:220].replace("\n"," ")
    return report.add(Result(name,group,status,r.latency_ms,r.status,detail or body,header_metrics(r.headers)))

def cleanup(api:API,attachment_id:str|None,headers:dict[str,str]) -> None:
    if not attachment_id: return
    with contextlib.suppress(Exception): api.request("DELETE",f"/guest/attachments/{urllib.parse.quote(attachment_id)}",headers=headers)

def run_api_suite(api:API,report:Report,seed_base:int|None=None) -> None:
    if seed_base is None: seed_base=random.SystemRandom().randrange(1000,59000)
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
        auto=chat.headers.get("x-daiki-auto-skills",""); profile=chat.headers.get("x-daiki-hermes-profile","")
        ok=chat.status==200 and DOC_MARKER in txt and "document" in auto.split(",") and profile=="guest-skills"
        report.add(Result("file-grounding","quality",PASS if ok else FAIL,chat.latency_ms,chat.status,f"auto={auto} profile={profile} {txt[:140]}",header_metrics(chat.headers)))
        down=api.request("GET",f"/guest/attachments/{aid}",headers=h)
        report.add(Result("file-download","file",PASS if down.status==200 and DOC_MARKER.encode() in down.body else FAIL,down.latency_ms,down.status,f"bytes={len(down.body)}",{}))
        cleanup(api,aid,h)

    # Exact production regression: a language-neutral English attachment sentence
    # must not switch an established Thai conversation to another language.
    lh=guest_headers(seed_base+15,"Loop Thai Attachment")
    lup=api.multipart("/guest/attachments",DOC_FIXTURE,lh,"file"); lud=json_obj(lup.body) or {}; laid=lud.get("id") if isinstance(lud,dict) else None
    if laid:
        lmessages=[
            {"role":"user","content":"ตอนนี้เราคุยกันเป็นภาษาไทย ช่วยตอบภาษาไทย"},
            {"role":"assistant","content":"ได้ครับ ผมจะตอบเป็นภาษาไทย"},
            {"role":"user","content":"Please review the attached content."},
        ]
        lchat=api.request("POST","/guest/chat",headers=lh,json_body={"model":"fast","attachmentIds":[laid],"messages":lmessages,"stream":False})
        ltxt=chat_text(lchat.body).strip(); lang=lchat.headers.get("x-daiki-response-language","")
        thai=bool(re.search(r"[\u0E00-\u0E7F]",ltxt)); han=len(re.findall(r"[\u4E00-\u9FFF]",ltxt))
        report.add(Result("thai-attachment-language","quality",PASS if lchat.status==200 and lang=="th-TH" and thai and han<8 else FAIL,lchat.latency_ms,lchat.status,f"lang={lang} thai={thai} han={han} {ltxt[:120]}",header_metrics(lchat.headers)))
        cleanup(api,laid,lh)
    else:
        report.add(Result("thai-attachment-language","quality",FAIL,lup.latency_ms,lup.status,"language fixture upload failed",{}))

    # Production document parsers: PDF, XLSX, and a large CSV target deep in the file.
    with tempfile.TemporaryDirectory(prefix="daiki-doc-e2e-") as td:
        td_path=pathlib.Path(td)
        pdf_path=td_path/"marker.pdf"; xlsx_path=td_path/"marker.xlsx"; csv_path=td_path/"large.csv"
        build_pdf_fixture(pdf_path); build_xlsx_fixture(xlsx_path); build_large_csv_fixture(csv_path)
        doc_cases=[
            ("pdf",pdf_path,"Return only the exact verification marker visible in the attached PDF.",PDF_MARKER,"pdf-ready","pdf"),
            ("xlsx",xlsx_path,"Return only the exact value beside verification in the attached spreadsheet.",XLSX_MARKER,"xlsx-ready","sheet"),
            ("large-csv",csv_path,f"For SKU {CSV_SKU}, return only the exact status value.",CSV_STATUS,"text-","csv"),
        ]
        for offset,(name,file_path,prompt,expected,status_prefix,expected_skill) in enumerate(doc_cases,50):
            dh=guest_headers(seed_base+offset,f"Loop {name}")
            dup=api.multipart("/guest/attachments",file_path,dh,"file")
            dd=json_obj(dup.body) or {}; daid=dd.get("id") if isinstance(dd,dict) else None; extract=str(dd.get("extractStatus",'')) if isinstance(dd,dict) else ""
            upload_ok=dup.status==201 and bool(daid) and extract.startswith(status_prefix)
            report.add(Result(f"{name}-upload","document",PASS if upload_ok else FAIL,dup.latency_ms,dup.status,f"id={daid} extract={extract} bytes={file_path.stat().st_size}",{}))
            if daid:
                dchat=api.request("POST","/guest/chat",headers=dh,json_body={"model":"fast","attachmentIds":[daid],"messages":[{"role":"user","content":prompt}],"stream":False})
                dtxt=chat_text(dchat.body).strip()
                auto=dchat.headers.get("x-daiki-auto-skills",""); profile=dchat.headers.get("x-daiki-hermes-profile","")
                ok=dchat.status==200 and expected in dtxt and expected_skill in auto.split(",") and profile=="guest-skills"
                report.add(Result(f"{name}-grounding","document",PASS if ok else FAIL,dchat.latency_ms,dchat.status,f"auto={auto} profile={profile} {dtxt[:140]}",header_metrics(dchat.headers)))
                cleanup(api,daid,dh)
    # Native image understanding. Marker exists only in pixels.
    h=guest_headers(seed_base+20,"Loop Vision")
    up=api.multipart("/guest/attachments",VISION_FIXTURE,h,"image"); ud=json_obj(up.body) or {}; aid=ud.get("id") if isinstance(ud,dict) else None
    report.add(Result("image-upload","image",PASS if up.status==201 and aid else FAIL,up.latency_ms,up.status,str(aid or up.body[:100]),{}))
    if aid:
        chat=api.request("POST","/guest/chat",headers=h,json_body={"model":"fast","attachmentIds":[aid],"messages":[{"role":"user","content":"Read the exact large alphanumeric code visible in the attached image. Return only the code."}],"stream":False}); txt=chat_text(chat.body).strip()
        auto=chat.headers.get("x-daiki-auto-skills",""); profile=chat.headers.get("x-daiki-hermes-profile",""); alias=chat.headers.get("x-daiki-model-alias","")
        ok=chat.status==200 and txt==VISION_MARKER and "image" in auto.split(",") and profile=="vision" and alias=="vision"
        report.add(Result("native-vision","quality",PASS if ok else FAIL,chat.latency_ms,chat.status,f"auto={auto} profile={profile} alias={alias} {txt[:120]}",header_metrics(chat.headers)))
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
    elif ig.status==429 and isinstance(idata,dict) and idata.get("error")=="guest_image_generation_limit":
        report.add(Result("image-generation","generation",BLOCKED,ig.latency_ms,ig.status,"Guest daily image-generation policy limit reached for this test identity",{}))
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
    # kubectl exec starts as container root in the s6 image while the gateway itself
    # runs as uid/gid 10000. Run probes as the production identity so the loop cannot
    # leave root-owned logs/cache/session files in shared profile PVCs.
    wrapped=f"exec setpriv --reuid=10000 --regid=10000 --init-groups sh -lc {shlex.quote(shell_script)}"
    return run_cmd(["kubectl","--context",context,"-n",namespace,"exec",pod,"--","sh","-lc",wrapped],timeout=timeout)

def run_hermes_suite(context:str,namespace:str,report:Report)->None:
    pod=ready_hermes_pod(context,namespace)
    budgets={
        "user":{"maxTools":2,"maxToolBytes":3000,"maxSkillsBytes":200},
        "skills":{"maxTools":3,"maxToolBytes":4200,"maxSkillsBytes":2200},
        "agent":{"maxTools":1,"maxToolBytes":5000,"maxSkillsBytes":200},
        "research":{"maxTools":0,"maxToolBytes":64,"maxSkillsBytes":200},
        "vision":{"maxTools":3,"maxToolBytes":4200,"maxSkillsBytes":900},
        "guest":{"maxTools":0,"maxToolBytes":64,"maxSkillsBytes":200},
        "guest-skills":{"maxTools":3,"maxToolBytes":4200,"maxSkillsBytes":2200},
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
    vision_script = """HERMES_HOME=/opt/data/profiles/vision /opt/hermes/.venv/bin/python - <<'PYV'
from agent.vision_message_prep import VisionMessagePrepMixin
from agent.image_routing import decide_image_input_mode
from hermes_cli.config import load_config
class Probe(VisionMessagePrepMixin):
    provider = "custom"
    model = "groq-qwen-qwen3.8-27b"
    _anthropic_image_fallback_cache = {}
p = Probe()
msg = [{"role":"user","content":[{"type":"text","text":"review"},{"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgo="}}]}]
cfg = load_config()
out = p._prepare_messages_for_non_vision_model(msg)
print("supportsVision=", p._model_supports_vision())
print("mode=", decide_image_input_mode(p.provider,p.model,cfg))
print("unchanged=", out == msg)
PYV"""
    code,out,ms=kubectl_exec(context,namespace,pod,vision_script,90)
    vision_ok=code==0 and "supportsVision= True" in out and "mode= native" in out and "unchanged= True" in out
    report.add(Result("native-vision-routing","hermes",PASS if vision_ok else FAIL,ms,None,out[-240:].replace("\n"," "),{}))
    inventory_script='printf "files="; find /opt/data/profiles/skills/skills -name SKILL.md 2>/dev/null | wc -l; printf "custom="; for s in daiki-document-analysis daiki-image-analysis graft-code-intelligence; do find /opt/data/profiles/skills/skills -path "*/$s/SKILL.md" -print -quit 2>/dev/null; done | wc -l'
    inv_code,inv_out,inv_ms=kubectl_exec(context,namespace,pod,inventory_script,90)
    inv_match=re.search(r"files=\s*(\d+).*custom=\s*(\d+)",inv_out,re.S)
    guest_inventory_script='printf "guestCustom="; for s in daiki-document-analysis daiki-image-analysis graft-code-intelligence; do find /opt/data/profiles/guest-skills/skills -path "*/$s/SKILL.md" -print -quit 2>/dev/null; done | wc -l'
    guest_inv_code,guest_inv_out,guest_inv_ms=kubectl_exec(context,namespace,pod,guest_inventory_script,90)
    guest_inv_match=re.search(r"guestCustom=\s*(\d+)",guest_inv_out)
    vision_inventory_script='printf "visionCustom="; find /opt/data/profiles/vision/skills -path "*/daiki-image-analysis/SKILL.md" -print -quit 2>/dev/null | wc -l'
    vision_inv_code,vision_inv_out,vision_inv_ms=kubectl_exec(context,namespace,pod,vision_inventory_script,90)
    vision_inv_match=re.search(r"visionCustom=\s*(\d+)",vision_inv_out)
    journey_script='HERMES_HOME=/opt/data/profiles/skills /opt/hermes/.venv/bin/hermes journey --json 2>/dev/null'
    journey_code,journey_out,journey_ms=kubectl_exec(context,namespace,pod,journey_script,90)
    journey_nodes=0
    with contextlib.suppress(Exception):
        raw=journey_out[journey_out.find("{"):]
        journey_nodes=len(json.loads(raw).get("nodes",[]))
    metrics={"skillFiles":int(inv_match.group(1)) if inv_match else 0,"customSkills":int(inv_match.group(2)) if inv_match else 0,"guestCustomSkills":int(guest_inv_match.group(1)) if guest_inv_match else 0,"visionCustomSkills":int(vision_inv_match.group(1)) if vision_inv_match else 0,"journeyNodes":journey_nodes}
    inventory_ok=inv_code==0 and guest_inv_code==0 and vision_inv_code==0 and journey_code==0 and metrics["skillFiles"]>=53 and metrics["customSkills"]==3 and metrics["guestCustomSkills"]==3 and metrics["visionCustomSkills"]==1
    report.add(Result("skill-inventory","learning",PASS if inventory_ok else FAIL,inv_ms+guest_inv_ms+vision_inv_ms+journey_ms,None,f"files={metrics['skillFiles']} custom={metrics['customSkills']} guestCustom={metrics['guestCustomSkills']} visionCustom={metrics['visionCustomSkills']} journey={metrics['journeyNodes']}",metrics))
    cases=[
        ("user","plain-intelligence","Reply exactly HERMES_USER_OK","HERMES_USER_OK",None),
        ("skills","skill-graft","Use skill_view to inspect the installed skill named graft-code-intelligence, then answer with GRAFT_SKILL_OK followed by one token-saving principle from that skill.","GRAFT_SKILL_OK","skills"),
        ("vision","vision-skill-image","Use skill_view to inspect the installed skill named daiki-image-analysis, then answer with VISION_IMAGE_SKILL_OK followed by one image-grounding rule.","VISION_IMAGE_SKILL_OK","skills"),
        ("guest-skills","guest-skill-graft","Use skill_view to inspect the installed skill named graft-code-intelligence, then answer with GUEST_GRAFT_SKILL_OK followed by one safe repository-analysis principle. Do not use any shell or filesystem tool.","GUEST_GRAFT_SKILL_OK","skills"),
        ("guest-skills","guest-skill-document","Use skill_view to inspect the installed skill named daiki-document-analysis, then answer with GUEST_DOCUMENT_SKILL_OK followed by one bounded attachment rule. Do not use any shell or filesystem tool.","GUEST_DOCUMENT_SKILL_OK","skills"),
        ("guest-skills","guest-skill-image","Use skill_view to inspect the installed skill named daiki-image-analysis, then answer with GUEST_IMAGE_SKILL_OK followed by one image-grounding rule. Do not use any shell or filesystem tool.","GUEST_IMAGE_SKILL_OK","skills"),
        ("skills","skill-document","Use skill_view to inspect the installed skill named daiki-document-analysis, then answer with DOCUMENT_SKILL_OK followed by one rule for bounded attachment excerpts.","DOCUMENT_SKILL_OK","skills"),
        ("agent","delegation","Use delegate_task exactly once for the tiny task 'What is 6 times 7?'. After it returns, answer exactly AGENT_DELEGATION_OK 42.","AGENT_DELEGATION_OK","delegation"),
    ]
    for profile,name,prompt,needle,toolsets in cases:
        token=uuid.uuid4().hex[:12]; usage=f"/tmp/daiki-loop-{token}.json"; home=f"/opt/data/profiles/{profile}"
        toolarg=f" -t {toolsets}" if toolsets else ""
        platform="HERMES_SESSION_PLATFORM=api_server " if profile in {"skills","guest-skills","vision"} else ""
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
    covered=["GET /health/live","GET /health/ready","GET /guest/policy","GET /guest/capabilities","POST /guest/chat","POST /guest/chat/stream","GET /guest/attachments","POST /guest/attachments","GET /guest/attachments/{id}","DELETE /guest/attachments/{id}","POST /guest/generate/file","POST /guest/generate/image"]
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
