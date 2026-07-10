#!/usr/bin/env bash
# 把 data/douyin/ 里**已转写**的医生账号增量入甲方 sibada-medical 知识库。
# 一条命令跑完：clone 甲方 KB → 生成口播内容层 → 死链校验 → commit → push → 拉平 per-user clone。
#
# 幂等：同数据重跑无改动即 "nothing to ingest"。增量：新账号转写好后放进 data/douyin/ 直接重跑。
# 只入有 transcripts-corrected.json 的账号；仅元数据（未转写）的会被跳过并列出。
#
# 前提：本机有 python3 + ssh 能连甲方 ECS（默认 alias sibada-prod）。
# 用法：bash scripts/ingest-kb/ingest.sh [--dry-run]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$REPO_ROOT/data/douyin}"
PROD_SSH="${PROD_SSH:-sibada-prod}"                       # 甲方 ECS ssh alias
BARE="${BARE:-/mnt/nas/sibada/_shared/kb/sibada-kb.git}"  # 甲方 NAS KB canonical bare repo
NAS_USERS="${NAS_USERS:-/mnt/nas/sibada}"                 # per-user clone 根（<uid>/kb/sibada-medical）
KB_SLUG="${KB_SLUG:-sibada-medical}"
GIT_NAME="${GIT_NAME:-Jerry Zhang}"
GIT_EMAIL="${GIT_EMAIL:-zhangjianye@gmail.com}"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
echo "==> clone 甲方 KB bare repo（${PROD_SSH}:${BARE}）"
git clone -q "ssh://$PROD_SSH/${BARE#/}" "$WORK/kb"
cd "$WORK/kb"; git config user.name "$GIT_NAME"; git config user.email "$GIT_EMAIL"

echo "==> 确保 index.md 口播层 marker 存在（首次运行自动包住已有区块，幂等）"
python3 - "$WORK/kb/index.md" << 'PYEOF'
import re, sys
p = sys.argv[1]
t = open(p).read()
if "douyin:doctors:begin" not in t:
    m = re.search(r"### 医生页（.*?待补[^\n]*。", t, re.S)
    if m: t = t.replace(m.group(0), "<!-- douyin:doctors:begin -->\n"+m.group(0)+"\n<!-- douyin:doctors:end -->")
if "douyin:status:begin" not in t:
    m = re.search(r"\| raw_md 口播文案 \|.*?\| 待补 \|[^\n]*\|", t, re.S)
    if m: t = t.replace(m.group(0), "<!-- douyin:status:begin -->\n"+m.group(0)+"\n<!-- douyin:status:end -->")
open(p, "w").write(t)
PYEOF

echo "==> 生成口播内容层"
python3 "$HERE/gen_content_layer.py" --data "$DATA_DIR" --kb "$WORK/kb"

echo "==> 死链校验（wiki/content + raw_md/口播文案）"
python3 - "$WORK/kb" << 'PYEOF'
import os, re, sys
kb = sys.argv[1]; bad = 0
for root, _, files in os.walk(kb):
    if "/.git" in root: continue
    for f in files:
        if not f.endswith(".md"): continue
        rel = os.path.relpath(os.path.join(root, f), kb)
        if not (rel.startswith("wiki/content") or rel.startswith("raw_md/口播文案")): continue
        for m in re.finditer(r"\]\((?!https?://)([^)]+\.md)\)", open(os.path.join(root, f)).read()):
            tgt = os.path.normpath(os.path.join(root, m.group(1).split("#")[0]))
            if not os.path.exists(tgt): print("死链:", rel, "->", m.group(1)); bad += 1
sys.exit(1 if bad else 0)
PYEOF

if git diff --quiet && git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
  echo "==> 无改动，nothing to ingest（数据未变或都已入库）"; exit 0
fi
echo "==> 本次改动："; git add -A; git status --short

if [ "$DRY" = 1 ]; then
  echo "==> --dry-run diff（不提交，对比甲方 KB 当前 main）："; git add -A; git --no-pager diff --cached HEAD --stat
  exit 0
fi

N_DOC=$(find "$WORK/kb/wiki/content/doctors" -name '*.md' | wc -l | tr -d ' ')
git commit -q -m "ingest: 口播内容层增量更新（issue #10，$(date +%F)）

生成器 scripts/ingest-kb/gen_content_layer.py 从 data/douyin/ 已转写账号重建。
当前 $N_DOC 位医生。仅元数据未转写的账号已跳过。"
echo "==> push 甲方 bare repo"
git push -q origin HEAD:main
PUSHED=$(git rev-parse --short HEAD)
echo "==> push 完成 ${PUSHED}，拉平 per-user clone + 修属主"
ssh "$PROD_SSH" "
  for d in $NAS_USERS/*/kb/$KB_SLUG; do
    [ -d \"\$d/.git\" ] || continue
    git -C \"\$d\" pull -q --rebase $BARE main >/dev/null 2>&1 && echo \"  \$(basename \$(dirname \$(dirname \$d))) -> \$(git -C \"\$d\" log -1 --format=%h)\"
  done
  sudo find $NAS_USERS/*/kb -user root ! -path '*_shared*' -exec chown 1000:1000 {} + 2>/dev/null || true
  echo '  bare HEAD: '\$(git --git-dir=$BARE log -1 --format='%h %s')
"
echo "==> 完成。"
