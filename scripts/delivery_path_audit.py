#!/usr/bin/env python3
"""Static self-check: delivery dataset execution fields must not contain host paths,
Java-8 commands must not carry Java 9+ module options, and any ./mvnw must resolve a
usable wrapper bootstrap (maven-wrapper.jar for jar-type, OR maven-wrapper.properties
with distributionType=only-script for script-type). Extends the existing dataset audit."""
import csv, glob, json, os, re, sys
DATASET="/opt/dataset/java/delivery_schema"
PROJECTS="/opt/projects"
HOST_PATTERNS=[r"/Users/", r"/home/[a-z][a-z0-9_-]+/", r"[A-Z]:\\\\"]
JAVA8_BAD=["--add-opens","--add-exports","-Djava.security.manager=allow"]
violations=[]

def resolve_cwd(project_name, cmd):
    base=os.path.join(PROJECTS, project_name); cwd=base
    for m in re.finditer(r"(?:^|[;])\s*cd\s+([^\s;]+)", cmd):
        d=m.group(1).strip().strip('"').strip("'")
        if d.startswith("/"):
            continue
        # Expand common project-root variables to the base, then append any /subdir.
        for var in ("${project_root}", "$project_root", "$PWD", "${PWD}"):
            if d.startswith(var):
                rest=d[len(var):].lstrip("/")
                cwd=os.path.join(base, rest) if rest else base
                break
        else:
            cwd=os.path.join(cwd, d)
    return cwd

def wrapper_ok(cwd):
    """A ./mvnw is usable offline if either the jar exists (jar-type) or
    maven-wrapper.properties declares distributionType=only-script (script-type)."""
    jar=os.path.join(cwd, ".mvn/wrapper/maven-wrapper.jar")
    if os.path.isfile(jar): return True
    props=os.path.join(cwd, ".mvn/wrapper/maven-wrapper.properties")
    if os.path.isfile(props):
        try:
            txt=open(props).read()
            if "distributionType=only-script" in txt or "distributionType=script" in txt:
                return True
        except: pass
    return False

rows=0
for f in sorted(glob.glob(DATASET+"/*.csv")):
    smell=os.path.basename(f)[:-4]
    for r in csv.DictReader(open(f,newline="",encoding="utf-8-sig")):
        rows+=1; sid=r.get("sample_id","")
        for col in ("project_path","test_location","test_command","focused_test_command"):
            v=r.get(col,"") or ""
            for pat in HOST_PATTERNS:
                if re.search(pat,v):
                    violations.append({"type":"HOST_PATH","smell":smell,"sample_id":sid,"column":col,"pattern":pat,"value":v[:120]})
            if any(k in v for k in JAVA8_BAD):
                violations.append({"type":"JDK8_BAD_OPTION","smell":smell,"sample_id":sid,"column":col,"options":[k for k in JAVA8_BAD if k in v]})
            if "./mvnw" in v:
                proj=r.get("project_name",""); cwd=resolve_cwd(proj, v)
                if not wrapper_ok(cwd):
                    violations.append({"type":"MVNW_WITHOUT_BOOTSTRAP","smell":smell,"sample_id":sid,"column":col,"project":proj})
# scan delivered test sources for /Users/a1-6
src_viol=[]
for proj in os.listdir(PROJECTS):
    pdir=os.path.join(PROJECTS,proj)
    if not os.path.isdir(pdir): continue
    for root,_,files in os.walk(pdir):
        if "/.git/" in root+"/": continue
        for fn in files:
            if not fn.endswith(".java"): continue
            fp=os.path.join(root,fn)
            try:
                for i,line in enumerate(open(fp,errors="ignore")):
                    if "/Users/a1-6" in line: src_viol.append({"project":proj,"file":fp,"line":i+1})
            except: pass
result={"dataset_rows_scanned":rows,"execution_field_violations":len(violations),
        "violations":violations,"test_source_host_path_violations":len(src_viol),
        "source_violations_sample":src_viol[:20],"passed":len(violations)==0 and len(src_viol)==0}
try: json.dump(result,open("/tmp/delivery_runtime_path_audit.json","w"),indent=2)
except Exception as e: print("WARN write:",e)
print(json.dumps({"passed":result["passed"],"exec_violations":len(violations),"src_violations":len(src_viol)},indent=2))
sys.exit(0 if result["passed"] else 1)
