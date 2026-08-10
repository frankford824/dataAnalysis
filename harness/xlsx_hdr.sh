#!/usr/bin/env bash
# Read the header row of a huge remote xlsx without transferring the whole file:
# streams a prefix of xl/worksheets/sheetN.xml + xl/sharedStrings.xml out of the zip.
# usage: xlsx_hdr.sh 'D:\path\big.xlsx' [sheetIndex] [sheetBytes] [ssBytes]
set -u
P="$1"; SI="${2:-1}"; SB="${3:-300000}"; SSB="${4:-3000000}"
read -r -d '' PSSRC <<EOF || true
[Console]::OutputEncoding=[Text.Encoding]::UTF8
\$ProgressPreference="SilentlyContinue"
Add-Type -AssemblyName System.IO.Compression.FileSystem
\$z=[IO.Compression.ZipFile]::OpenRead('$P')
\$names=(\$z.Entries | ForEach-Object { \$_.FullName }) -join ';'
"NAMES:\$names"
\$sheets=[regex]::Matches((New-Object IO.StreamReader(\$z.GetEntry('xl/workbook.xml').Open())).ReadToEnd(),'<sheet name="([^"]+)"') | ForEach-Object { \$_.Groups[1].Value }
"SHEETS:" + (\$sheets -join ';')
function Grab(\$n,\$max){
  \$e=\$z.GetEntry(\$n); if(\$e -eq \$null){ return "" }
  \$s=\$e.Open(); \$b=New-Object byte[] \$max; \$tot=0
  while(\$tot -lt \$max){ \$r=\$s.Read(\$b,\$tot,\$max-\$tot); if(\$r -le 0){break}; \$tot+=\$r }
  \$s.Close(); [Convert]::ToBase64String(\$b,0,\$tot)
}
"SHEETXML:" + (Grab "xl/worksheets/sheet$SI.xml" $SB)
"SS:" + (Grab "xl/sharedStrings.xml" $SSB)
\$z.Dispose()
EOF
ENC=$(printf '%s' "$PSSRC" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
timeout "${TMO:-170}" ssh -o BatchMode=yes -i ~/.ssh/finance_agent_deploy sxf@192.168.0.155 \
  "powershell -NoProfile -EncodedCommand $ENC" 2>/dev/null | \
/home/wsfwk/dataAnalysis/harness/.venv/bin/python -c "
import sys,base64,re
buf={}
cur=None
for line in sys.stdin:
    line=line.rstrip('\r\n')
    m=re.match(r'^(NAMES|SHEETS|SHEETXML|SS):(.*)\$',line)
    if m: cur=m.group(1); buf[cur]=m.group(2)
    elif cur: buf[cur]+=line
print('sheets:',buf.get('SHEETS','')) 
ss=[]
if buf.get('SS'):
    x=base64.b64decode(buf['SS']).decode('utf-8',errors='ignore')
    ss=[re.sub(r'<[^>]+>','',m) for m in re.findall(r'<si>(.*?)</si>',x,re.S)]
print('sharedStrings loaded:',len(ss))
sx=base64.b64decode(buf.get('SHEETXML','')).decode('utf-8',errors='ignore')
rows=re.findall(r'<row[^>]*>(.*?)</row>',sx,re.S)
print('rows parsed:',len(rows))
CELL=re.compile(r'<c\b([^>]*?)(?:/>|>(.*?)</c>)',re.S)
for ri,r in enumerate(rows[:int(__import__('os').environ.get('NROWS','3'))]):
    out=[]
    for attrs,inner in CELL.findall(r):
        t=(re.search(r'\bt=\"([^\"]+)\"',attrs) or [None,''])[1] if re.search(r'\bt=\"([^\"]+)\"',attrs) else ''
        ref=(re.search(r'\br=\"([A-Z]+)\d+\"',attrs).group(1) if re.search(r'\br=\"([A-Z]+)\d+\"',attrs) else '')
        inner=inner or ''
        if t=='inlineStr':
            iv=re.findall(r'<t[^>]*>(.*?)</t>',inner,re.S); val=''.join(iv)
        else:
            v=re.search(r'<v>(.*?)</v>',inner,re.S); val=v.group(1) if v else ''
            if t=='s' and val.isdigit() and int(val)<len(ss): val=ss[int(val)]
        out.append((ref,val))
    print('--- row%d (%d cells) ---'%(ri,len(out)))
    for ref,v in out: print('   [%3s] %s'%(ref,v[:50]))
"
