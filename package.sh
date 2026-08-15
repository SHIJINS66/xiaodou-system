#!/usr/bin/env bash
# =============================================================================
# companion-framework 打包脚本
# 把仓库打成可分发的 tar.gz（排除实例/密钥/缓存/临时文件），
# 并做发布前完整性自检（含敏感信息扫描，防止 key 泄漏进包）。
#
# 用法:
#   ./package.sh [--out DIR] [--name NAME]   # 打包到 DIR/NAME.tar.gz（默认 dist/）
#   ./package.sh --check                     # 只做发布前自检，不打包
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

OUT_DIR="${OUT_DIR:-$ROOT/dist}"
PKG_NAME="companion-framework-$(date +%Y%m%d-%H%M%S)"
ONLY_CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --name) PKG_NAME="$2"; shift 2 ;;
    --check) ONLY_CHECK=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

say()  { printf '\033[1;32m[package]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[package!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[package!!]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------- 发布前自检 ----------------
say "运行发布前自检…"

# 1. 全部 Python 编译
ERR=0
while IFS= read -r f; do
  python3 -m py_compile "$f" || { warn "编译失败: $f"; ERR=1; }
done < <(find . -name '*.py' -not -path '*/__pycache__/*')
[[ $ERR -eq 0 ]] && say "  全部 Python 编译通过" || die "存在编译失败"

# 2. shell 语法
bash -n init.sh && say "  init.sh 语法 OK"
bash -n package.sh && say "  package.sh 语法 OK"

# 3. 敏感信息扫描（真实 key / 线上路径 / 真名）
echo ""
say "敏感信息扫描："
grep -rIn -E "sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|/opt/xiaodou|/root/\.openclaw|时进|小豆" \
  --include='*.py' --include='*.sh' --include='*.md' --include='*.yaml' \
  --include='*.json' --include='*.txt' . || true
if grep -rIn -E "sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}" \
  --include='*.py' --include='*.sh' --include='*.yaml' --include='*.json' . >/dev/null 2>&1; then
  die "检测到疑似真实密钥，中止打包"
fi
say "  未发现疑似真实密钥"

# 4. 版本占位符残留检查
# 检查核心模板里是否残留未替换的安装路径占位（应只剩 {character_name} 等渲染占位）
echo ""
say "占位符检查（核心模板/运行代码只应有 render_prompt 支持的占位）":
grep -rIn "INSTALL_ROOT\|INSTANCE_ROOT\|/root/\.openclaw\|/opt/xiaodou" \
  --include='*.md' --include='*.py' --include='*.yaml' --include='*.sh' \
  scripts/ providers/ *.md settings.example.yaml init.sh prompts/ schemas/ 2>/dev/null || true

# 5. 必要文件存在（含 7 个核心模板）
CORE_TEMPLATES=(AGENTS.md IDENTITY.md SOUL.md LIFE.md USER.md TOOLS.md MEMORY.md)
for f in init.sh README.md LICENSE requirements.txt settings.example.yaml scripts/guided_setup.py; do
  [[ -f "$f" ]] || { die "必要文件缺失: $f"; }
done
for t in "${CORE_TEMPLATES[@]}"; do
  [[ -f "$t" ]] || { die "核心模板缺失: $t"; }
done
say "  必要文件（含 7 个核心模板）齐全"

if [[ $ONLY_CHECK -eq 1 ]]; then
  say "自检通过 ✓ （未打包）"
  exit 0
fi

# ---------------- 打包 ----------------
mkdir -p "$OUT_DIR"
TARBALL="$OUT_DIR/$PKG_NAME.tar.gz"
say "打包到 $TARBALL …"

# 用 tar 排除实例/密钥/缓存/文档草稿（*.md 只保留顶层 README 与各模板目录的 README）
# 排除：instance*、.env*、__pycache__、daily/chatlog/自拍/state/logs、dist、git
tar -czf "$TARBALL" \
  --exclude='instance*' \
  --exclude='.env' --exclude='.env.*' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='daily' --exclude='chatlog' --exclude='daily_selfies' \
  --exclude='state' --exclude='logs' \
  --exclude='dist' \
  --exclude='.git' \
  . || die "打包失败"

# 顶层发布的 *.md 只保留 README（去掉迁移/审计草稿，那些留在仓库但不进发布包）
# 重新打一个干净的归档：把顶层非 README 的 .md 也排除
rm -f "$TARBALL"
tar -czf "$TARBALL" \
  --exclude='instance*' \
  --exclude='.env' --exclude='.env.*' \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='daily' --exclude='chatlog' --exclude='daily_selfies' \
  --exclude='state' --exclude='logs' --exclude='dist' --exclude='.git' \
  --exclude='MIGRATION_AUDIT_FULL.md' --exclude='MIGRATION_CHECKLIST.md' \
  --exclude='HARDCODE_AUDIT.md' --exclude='P1_DEIDENTIFY.md' \
  . || die "打包失败"

SIZE=$(du -h "$TARBALL" | cut -f1)
FILE_CNT=$(tar -tzf "$TARBALL" | grep -v '/$' | wc -l)
say "打包完成：$TARBALL（$SIZE，$FILE_CNT 个文件）"
echo ""
say "内容清单："
tar -tzf "$TARBALL" | grep -v '/$' | sort | head -60
echo ""
say "验证：解压检查完整性"
TMPV=$(mktemp -d)
tar -xzf "$TARBALL" -C "$TMPV"
([[ -f "$TMPV/init.sh" && -f "$TMPV/settings.example.yaml" && -f "$TMPV/scripts/guided_setup.py" ]] \
  && say "  解压完整性通过") || die "解压完整性失败"
rm -rf "$TMPV"
say "发布包就绪 ✓"
