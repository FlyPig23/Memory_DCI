#!/usr/bin/env python3
"""Real public-network smoke in a synthetic task; no model calls or test content."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[4]
sys.path.insert(0,str(ROOT))
from experiment.benchmarks.wildclaw_bench.src.runtime_support import make_runtime_config
from experiment.shared.task_runtime import TaskRuntime,TaskSpec

PROBE = r'''
import asyncio,hashlib,importlib.util,json,os,pathlib,shutil,socket,subprocess,sys,time,urllib.request
root=pathlib.Path("/tmp_workspace")
report={"no_model_calls":True,"checks":{},"versions":{}}
def run(name,action):
    started=time.monotonic()
    try:
        result=action()
        report["checks"][name]={"passed":True,"seconds":time.monotonic()-started,**result}
    except Exception as exc:
        report["checks"][name]={"passed":False,"seconds":time.monotonic()-started,
                                "error_type":type(exc).__name__,"error":str(exc)[:1500]}
def direct_denied():
    try:
        sock=socket.create_connection(("1.1.1.1",443),timeout=2)
    except OSError as exc:
        return {"blocked":True,"error_type":type(exc).__name__}
    sock.close()
    raise RuntimeError("unexpected direct TCP connectivity")
def https():
    with urllib.request.urlopen("https://example.com",timeout=30) as response:
        body=response.read(20000).decode()
        assert response.status==200 and "Example Domain" in body
        return {"status":response.status,"url":response.url,"tls_verification":"default CA bundle"}
def academic():
    spec=importlib.util.spec_from_file_location("academic_probe","/root/.codex/skills/academic-literature-search/agent.py")
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    async def request():
        async with module.LiteratureSearchEngine() as engine:
            assert engine.session.trust_env
            async with engine.session.get("https://example.com",timeout=30) as response:
                body=await response.text()
                assert response.status==200 and "Example Domain" in body
                return {"status":response.status,"trust_env":engine.session.trust_env,"tls_verification":True}
    return asyncio.run(request())
def search():
    attempts=[]
    for backend in ("google","bing","duckduckgo"):
        command=["python3","/root/.codex/skills/ddgs-search/scripts/search.py",
                 "-q","Python official documentation","-m","3","-b",backend]
        result=subprocess.run(command,capture_output=True,text=True,timeout=100)
        (root/("ddgs-"+backend+".stdout.log")).write_text(result.stdout)
        (root/("ddgs-"+backend+".stderr.log")).write_text(result.stderr)
        try:
            data=json.loads(result.stdout)
        except ValueError:
            data={"error":result.stdout[-500:]+result.stderr[-500:]}
        attempts.append({"backend":backend,"exit":result.returncode,
                         "result_count":len(data.get("results",[])),"error":data.get("error")})
        report["search_backend_attempts"]=attempts
        if result.returncode==0 and data.get("results"):
            return {"provider":data["provider"],"backend":backend,
                    "source_urls":[r["url"] for r in data["results"]],
                    "query":"Python official documentation"}
    raise RuntimeError("No tested public backend returned sources: "+json.dumps(attempts)[:1500])
def ddgs_api():
    from ddgs.http_client import HttpClient
    response=HttpClient(proxy=os.environ["DDGS_PROXY"],verify="/opt/network-ca.pem").get("https://example.com")
    assert response.status_code==200 and "Example Domain" in response.text
    return {"status":response.status_code,"verify":"/opt/network-ca.pem","library":"ddgs/primp"}
def browser():
    executable="/ms-playwright/chromium-1208/chrome-linux64/chrome"
    certutil=shutil.which("certutil") or "/opt/nss-tools/usr/bin/certutil"
    assert pathlib.Path("/root/.pki/nssdb/cert9.db").exists(), "Shared helper did not prepare NSS"
    prefix=["agent-browser","--json","--proxy",os.environ["AGENT_BROWSER_PROXY"],
            "--executable-path",executable]
    logs=[]
    try:
        for arguments in [["open","https://example.com"],["get","title"],["screenshot","/tmp_workspace/example.png"]]:
            result=subprocess.run(prefix+arguments,capture_output=True,text=True,timeout=80)
            logs.append({"arguments":arguments,"exit":result.returncode,"stdout":result.stdout,"stderr":result.stderr})
            if result.returncode:
                raise RuntimeError("agent-browser failed: "+result.stdout[-1000:]+" "+result.stderr[-1000:])
            value=json.loads(result.stdout)
            if not value.get("success"):
                raise RuntimeError("agent-browser returned failure: "+result.stdout[-1000:])
        path=root/"example.png"
        assert path.exists() and path.stat().st_size>100
        return {"url":"https://example.com","screenshot":"example.png",
                "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                "certutil_present":bool(certutil),"ignore_https_errors":False}
    finally:
        (root/"browser.commands.json").write_text(json.dumps(logs,indent=2))
        subprocess.run(["agent-browser","close"],capture_output=True,text=True,timeout=30)
run("direct_network_denied",direct_denied)
run("https_ca_verified",https)
run("academic_aiohttp_ca_verified",academic)
run("ddgs_sources",search)
run("ddgs_api_explicit_ca",ddgs_api)
run("browser_proxy_ca_screenshot",browser)
report["passed"]=all(item["passed"] for item in report["checks"].values())
(root/"network_dependency_report.json").write_text(json.dumps(report,indent=2))
print(json.dumps(report))
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",default="hangxiao-skill-dci/wildclaw-codex:runtime-v2")
    args=parser.parse_args()
    run_id="dependency-network-"+str(int(time.time()))+"-"+uuid.uuid4().hex[:6]
    folder=ROOT/"experiment/benchmarks/wildclaw_bench/runs/smoke"/run_id
    inputs=folder/"synthetic-inputs"
    (inputs/"exec").mkdir(parents=True)
    (inputs/"exec/probe.py").write_text(PROBE)
    config=make_runtime_config(ROOT,args.image)
    task=TaskSpec(task_id="synthetic-network-dependencies",category="synthetic",
                  prompt="Synthetic dependency transport check. No model invocation.",
                  workspace_path=inputs,skills_root=ROOT/"experiment/benchmarks/wildclaw_bench/runtime/skills",
                  skills=("4/ddgs-search","4/academic-literature-search","agent-browser"))
    runtime=TaskRuntime(config,task,folder,run_id)
    result={"passed":False,"run_dir":str(folder.relative_to(ROOT)),"image":args.image,"network":"none"}
    try:
        runtime.prepare("")
        completed=runtime.exec(["python3","probe.py"],timeout=330,check=False)
        (folder/"probe.stdout.log").write_text(completed.stdout)
        (folder/"probe.stderr.log").write_text(completed.stderr)
        output=runtime.workspace/"network_dependency_report.json"
        if output.exists():
            result.update(json.loads(output.read_text()))
        else:
            result.update({"error":"probe did not produce report","exit_code":completed.returncode})
    except Exception as exc:
        result.update({"error_type":type(exc).__name__,"error":str(exc)[:1000]})
    finally:
        try:
            runtime.cleanup()
            result["owned_container_removed"]=not runtime.created
        except Exception as exc:
            result["cleanup_error"]=str(exc)[:500]
            result["passed"]=False
    (folder/"smoke_report.json").write_text(json.dumps(result,indent=2))
    (ROOT/"experiment/benchmarks/wildclaw_bench/manifests/network_dependency_smoke.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["passed"] else 1

if __name__=="__main__": raise SystemExit(main())
