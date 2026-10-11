"""Module simulation protocol in a separate process, JSON only."""
import contextlib
import json
import os
import runpy
import sys
from pathlib import Path
from pioneer.importscope import ImportScope

if sys.platform=='linux':
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(96*1024**2,96*1024**2))
root=Path(sys.argv[1]);file=sys.argv[2]
output=sys.stdout
with ImportScope(root).activate(),contextlib.redirect_stdout(sys.stderr):
    namespace=runpy.run_path(str(root/file))
    instance=None
    for line in sys.stdin:
        try:
            body=json.loads(line)
            if body['method']=='init':instance=namespace['Simulation'](body['args'][0]);result=True
            else:result=getattr(instance,body['method'])(*body['args'])
            reply={'ok':True,'result':result}
        except Exception as e:reply={'ok':False,'error':type(e).__name__}
        output.write(json.dumps(reply,ensure_ascii=False)+'\n');output.flush()
