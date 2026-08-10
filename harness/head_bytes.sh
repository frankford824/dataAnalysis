#!/usr/bin/env bash
# Read first N bytes of a remote file and decode locally (tries utf-8-sig/gbk/utf-16).
# usage: head_bytes.sh 'D:\path\file.csv' [bytes] [lines]
set -u
P="$1"; N="${2:-6000}"; L="${3:-4}"
PS="\$fs=[IO.File]::Open('$P','Open','Read','ReadWrite'); \$b=New-Object byte[] $N; \$n=\$fs.Read(\$b,0,$N); \$fs.Close(); [Convert]::ToBase64String(\$b,0,\$n)"
ENC=$(printf '%s' "$PS" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
timeout "${TMO:-150}" ssh -o BatchMode=yes -i ~/.ssh/finance_agent_deploy sxf@192.168.0.155 \
  "powershell -NoProfile -EncodedCommand $ENC" 2>/dev/null | tr -d '\r\n' | \
L="$L" /home/wsfwk/dataAnalysis/harness/.venv/bin/python -c "
import sys,base64,os
raw=base64.b64decode(sys.stdin.read())
enc=None
for e in ('utf-8-sig','utf-16','gbk','cp936','latin1'):
    try:
        t=raw.decode(e); enc=e; break
    except Exception: pass
print('# encoding guess:',enc,' bytes:',len(raw))
lines=t.splitlines()
for i,l in enumerate(lines[:int(os.environ['L'])]):
    sep='\t' if l.count('\t')>l.count(',') else ','
    cols=l.split(sep)
    print('--- line%d (%d cols, sep=%s) ---'%(i,len(cols),'TAB' if sep=='\t' else 'COMMA'))
    for j,c in enumerate(cols): print('   [%02d] %s'%(j,c[:60]))
    if i==0 and len(lines)>1: pass
"
