#!/usr/bin/env bash
# 端到端实测：把真实数据交上去，同时量界面在算账期间还答不答话。
#
# 后者才是重点。上传接口以前是 async def，里面同步跑完解析加重算，事件循环被
# 独占——一个人交表，所有人的界面转圈。改成同步函数丢线程池之后，这个脚本应该
# 看到上传还在跑、总览接口照样毫秒级返回。
set -euo pipefail

B=${B:-http://192.168.0.155:8000}
CORPUS=${CORPUS:-/home/wsfwk/data/platform}
TOKENS=$(dirname "$0")/.tokens.local.txt
ADMIN=$(awk -F'\t' '$2=="admin"{print $3}' "$TOKENS")

args=()
n=0
while IFS= read -r f; do
  args+=(-F "files=@$f")
  n=$((n + 1))
done < <(find "$CORPUS" -type f -name '*.xlsx' | sort)
echo "交 $n 个文件，共 $(du -shc $(find "$CORPUS" -type f -name '*.xlsx') | tail -1 | cut -f1)"

out=$(mktemp)
start=$(date +%s)
curl -s -o "$out" -w '%{http_code} %{time_total}' \
  -H "Authorization: Bearer $ADMIN" "${args[@]}" "$B/api/upload" > /tmp/upload_meta & 
up=$!

echo
echo "上传在跑，期间每 2 秒打一次总览接口："
slow=0
while kill -0 $up 2>/dev/null; do
  t=$(curl -s -o /dev/null -w '%{time_total}' -H "Authorization: Bearer $ADMIN" "$B/api/overview" || echo 999)
  ms=$(python3 -c "print(int(float('$t')*1000))")
  mark=''
  if [ "$ms" -gt 2000 ]; then mark='  <- 卡了'; slow=$((slow + 1)); fi
  printf '  第 %2d 秒  总览 %5d ms%s\n' "$(( $(date +%s) - start ))" "$ms" "$mark"
  sleep 2
done
wait $up || true

echo
echo "上传结果： $(cat /tmp/upload_meta)"
python3 - "$out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
print("  " + d.get("summary", ""))
print(f"  收下 {len(d.get('kept', []))} 份，退回 {len(d.get('rejected', []))} 份，"
      f"失败 {len(d.get('failures', []))} 处，认不出的表 {len(d.get('unknown_tables', []))} 张")
for r in d.get("rejected", [])[:5]:
    print(f"  退回：{r['file']} —— {r['why']}")
for f in d.get("failures", [])[:5]:
    print(f"  失败：{f}")
print("  账期：", d.get("periods"))
PY
echo
echo "算账期间总览接口超过 2 秒的次数：$slow（应当是 0）"
