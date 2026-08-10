#!/usr/bin/env bash
# Peek at a remote xlsx/xlsm: fetch via base64 and dump sheet names + head rows.
# usage: xlsx_peek.sh 'D:\path\file.xlsx' [rows] [maxsheets]
set -u
P="$1"; ROWS="${2:-6}"; MAXS="${3:-40}"
PS="[Convert]::ToBase64String([IO.File]::ReadAllBytes('$P'))"
ENC=$(printf '%s' "$PS" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
timeout "${TMO:-180}" ssh -o BatchMode=yes -i ~/.ssh/finance_agent_deploy sxf@192.168.0.155 \
  "powershell -NoProfile -EncodedCommand $ENC" 2>/dev/null | tr -d '\r\n' | \
ROWS="$ROWS" MAXS="$MAXS" /home/wsfwk/dataAnalysis/harness/.venv/bin/python -c "
import sys,base64,io,os,openpyxl
raw=base64.b64decode(sys.stdin.read())
print('bytes:',len(raw))
wb=openpyxl.load_workbook(io.BytesIO(raw),data_only=True,read_only=True)
print('sheets(%d):'%len(wb.sheetnames),wb.sheetnames[:int(os.environ['MAXS'])])
for ws in wb.worksheets[:int(os.environ['MAXS'])]:
    print('--- [%s] dims=%s ---'%(ws.title,ws.calculate_dimension()))
    for i,r in enumerate(ws.iter_rows(values_only=True)):
        if i>=int(os.environ['ROWS']): break
        print(' ',[str(x)[:26] if x is not None else '' for x in r][:16])
"
