#!/usr/bin/env bash
# =============================================================================
# companion-framework 安装器
# 从仓库克隆到新环境后执行：校验环境 → 复制模板到实例目录 → 生成 settings.yaml
# → 建目录 → 全量编译校验 → （可选）安装 cron。
#
# 用法:
#   ./init.sh [--instance-dir DIR] [--settings EXAMPLE_YAML] [--no-cron]
#
# 默认实例目录: ./instance
# 默认 settings 模板: ./settings.example.yaml
# 默认不安装 cron（用 --install-cron 显式开启，安装前会打印将追加的 cron 行）
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 实例目录 = 用户的 OpenClaw workspace。OpenClaw 默认从此读核心文件，
# 本框架的 step 系统与之共用。默认 ~/.openclaw/workspace（可用 --instance-dir 覆盖）。
INSTANCE_DIR="${INSTANCE_DIR:-$HOME/.openclaw/workspace}"
SETTINGS_TEMPLATE="${SETTINGS_TEMPLATE:-$ROOT/settings.example.yaml}"
INSTALL_CRON=0
TIMEZONE="${TIMEZONE:-Asia/Shanghai}"

usage() { echo "用法: $0 [--instance-dir DIR] [--install-cron] [--settings FILE]"; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance-dir) INSTANCE_DIR="$2"; shift 2 ;;
    --settings) SETTINGS_TEMPLATE="$2"; shift 2 ;;
    --install-cron) INSTALL_CRON=1; shift ;;
    --no-cron) INSTALL_CRON=0; shift ;;
    *) usage ;;
  esac
done

say()  { printf '\033[1;32m[init]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[init!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[init!!]\033[0m %s\n' "$*" >&2; exit 1; }

say "companion-framework 安装器"
say "  仓库根:      $ROOT"
say "  实例目录:    $INSTANCE_DIR"
say "  settings 模板: $SETTINGS_TEMPLATE"

# ---------------------------------------------------------------- 环境校验
command -v python3 >/dev/null 2>&1 || die "未找到 python3"
PYVER="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
say "  Python: $PYVER (需要 3.10+)"
[[ "$(python3 -c 'import sys;print(sys.version_info.major)')" == "3" ]] || die "需要 Python 3"

CRON_INSTALL=0
if [[ $INSTALL_CRON == 1 ]]; then
  # at 服务（step03 真实调度依赖）
  if command -v at >/dev/null 2>&1 && (systemctl is-active atd >/dev/null 2>&1 || service atd status >/dev/null 2>&1); then
    say "  atd 服务:  active"
    CRON_INSTALL=1
  else
    warn "at/atd 不可用 —— 将跳过 cron 安装（step03 at 调度无法运行）。"
  fi
fi

# 时区强校验：step02/03 的 cron 模板依赖 Asia/Shanghai 的 CRON_TZ+TZ 双重一致。
detected_tz="$(cat /etc/timezone 2>/dev/null || grep -oP '^TZ=\K.*' /etc/timezone 2>/dev/null || true)"
[[ -z "$detected_tz" ]] && detected_tz="$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##' || true)"
if [[ "${TZ:-}" != "" ]]; then detected_tz="$TZ"; fi
if [[ "$detected_tz" != "" && "$detected_tz" != "$TIMEZONE" ]]; then
  warn "系统时区 ($detected_tz) 与 settings ($TIMEZONE) 不一致。"
  warn "cron 的 CRON_TZ 和 at 的 TZ 必须与脚本 datetime.now(TZ) 三重一致，建议统一为 $TIMEZONE。"
fi

# ---------------------------------------------------------------- 复制模板
say "建实例目录结构"
mkdir -p "$INSTANCE_DIR"/{config,assets,daily,chatlog,memory,daily_selfies,logs,var/planner,var/state,var/journal,var/lock/events}

# ------------------------------------------------------------------
# 复制核心文件模板到 workspace：
#   - 已存在的文件（OpenClaw 自带的 AGENTS/IDENTITY/SOUL/USER/TOOLS，或用户已填的）
#     一律跳过，绝不覆盖；用户想自定义可直接上传同名文件到 workspace 替换。
#   - 缺失的文件（如 LIFE.md / MEMORY.md 这类 OpenClaw 不自带的）才复制通用模板。
# ------------------------------------------------------------------
CORE_TEMPLATES=(AGENTS.md IDENTITY.md SOUL.md LIFE.md USER.md TOOLS.md MEMORY.md)
for name in "${CORE_TEMPLATES[@]}"; do
  if [[ -f "$INSTANCE_DIR/$name" ]]; then
    say "跳过 $name（已存在，保留；如需自定义请上传同名文件覆盖）"
  elif [[ -f "$ROOT/$name" ]]; then
    cp "$ROOT/$name" "$INSTANCE_DIR/$name"
    say "复制核心模板 $name -> workspace"
  fi
done

# ---------------------------------------------------------------- settings
SETTINGS_OUT="$INSTANCE_DIR/settings.yaml"
if [[ -f "$SETTINGS_OUT" ]]; then
  say "settings.yaml 已存在: $SETTINGS_OUT （保留，不覆盖）"
else
  [[ -f "$SETTINGS_TEMPLATE" ]] || die "settings 模板不存在: $SETTINGS_TEMPLATE"
  cp "$SETTINGS_TEMPLATE" "$SETTINGS_OUT"
  # 把 root_dir 指向实例目录
  sed -i "s#root_dir: .*#root_dir: \"$INSTANCE_DIR\"#" "$SETTINGS_OUT" 2>/dev/null || true
  say "生成 settings.yaml: $SETTINGS_OUT"
  say "  >>> 请填写 .env 密钥（API key 等）后运行 verify"
fi

# ---------------------------------------------------------------- 编译校验
say "全量编译校验（scripts + providers）"
FAIL=0
while IFS= read -r -d '' f; do
  if ! python3 -m py_compile "$f" 2>"$INSTANCE_DIR/logs/compile.err"; then
    warn "编译失败: $f"; cat "$INSTANCE_DIR/logs/compile.err"; FAIL=1
  fi
done < <(find "$ROOT/scripts" "$ROOT/providers" -name "*.py" -print0)
if [[ $FAIL == 1 ]]; then die "存在编译错误，请检查 logs/compile.err"; fi
say "全部脚本编译通过"

# 依赖 import 校验（不启动任何网络/发送）
if python3 -c "
import sys; sys.path.insert(0,'$ROOT/scripts'); sys.path.insert(0,'$ROOT')
import common, providers.base
import at_adapter, schema_validator, deterministic_gate, event_transaction
import execute_daily_event, build_daily_plan, initialize_daily_file
import run_morning_pipeline, run_morning_pipeline_step03
import step04_config, finalize_day, finalize_yesterday, update_memory_md, session_rollover
import normalize_chatlog, render_chatlog, rollover_artifacts, reconcile_daily_state
import backup_openclaw, raw_backup, mirror_openclaw_memory
print('核心模块 import OK')
" >/dev/null 2>&1; then
  say "核心模块 import OK"
else
  warn "部分核心模块 import 失败（可能是依赖未装）——先 pip install -r $ROOT/requirements.txt"
fi

# ---------------------------------------------------------------- cron 安装
# 双形态：root 用户 → 系统 crontab（/etc/crontab，命令前有 user 字段）；
#         普通用户 → 用户级 crontab（crontab 命令，命令前无 user 字段）。
# 模板里的 {CRON_USER} 占位——root 形态替换成 "root"，普通形态删除。
if [[ $CRON_INSTALL == 1 ]]; then
  if [[ "$(id -u)" == "0" ]]; then
    CRON_MODE="system"
    CRON_USR="root"
  else
    CRON_MODE="user"
    CRON_USR=""
  fi
  say "生成并安装 cron（形态: $CRON_MODE）"
  CRON_FILE="$INSTANCE_DIR/var/crontab.generated"
  : > "$CRON_FILE"
  for TPL in "$ROOT"/cron/step0*.cron.template; do
    [[ -e "$TPL" ]] || continue
    # 跳过纯占位模板（无有效命令）
    if grep -q '当前暂无可用命令' "$TPL"; then
      warn "跳过占位模板: $(basename "$TPL")"
      continue
    fi
    sed \
      -e "s#{INSTALL_ROOT}#$ROOT#g" \
      -e "s#{SETTINGS}#$INSTANCE_DIR/settings.yaml#g" \
      -e "s#{LOCK}#$INSTANCE_DIR/var/lock#g" \
      -e "s#{LOG}#$INSTANCE_DIR/logs#g" \
      -e "s#{TIMEZONE}#$TIMEZONE#g" \
      -e "s#{PYTHON_BIN}#$(command -v python3)#g" \
      -e "s#{PATH}#$PATH#g" \
      "$TPL" >> "$CRON_FILE"
    echo "" >> "$CRON_FILE"
  done
  # 双形态：系统形态把 {CRON_USER} 换成 root；用户形态删掉该列（含前导空格）
  if [[ "$CRON_MODE" == "system" ]]; then
    sed -i -e "s#{CRON_USER}#root#g" "$CRON_FILE"
  else
    sed -i -e "s# {CRON_USER}##g" -e "s#^{CRON_USER}##g" "$CRON_FILE"
  fi
  say "生成 cron 到: $CRON_FILE"
  say "下面是要安装的内容："
  echo "-----------------------------------------------------------"
  cat "$CRON_FILE"
  echo "-----------------------------------------------------------"
  if [[ "$CRON_MODE" == "system" ]]; then
    if [[ "${ALLOW_CRON_APPLY:-0}" == "1" ]]; then
      cat "$CRON_FILE" >> /etc/crontab
      say "已追加到 /etc/crontab"
    else
      warn "未自动写入 /etc/crontab（ALLOW_CRON_APPLY=1 时才会写）。"
      warn "确认无误后手动执行: cat $CRON_FILE >> /etc/crontab"
    fi
  else
    # 普通用户：追加到用户级 crontab
    if [[ "${ALLOW_CRON_APPLY:-0}" == "1" ]]; then
      cat "$CRON_FILE" | crontab -
      say "已写入用户级 crontab（crontab -l 可查看）"
    else
      warn "未自动写入用户级 crontab（ALLOW_CRON_APPLY=1 时才会写）。"
      warn "确认无误后手动执行: cat $CRON_FILE | crontab -"
    fi
  fi
else
  say "跳过 cron 安装（用 --install-cron 开启）"
fi

say ""
say "安装完成。下一步："
say "  1. 编辑 $INSTANCE_DIR/settings.yaml 填 key/路径"
say "  2. 在 $INSTANCE_DIR/.env 填 API key"
say "  3. 运行 verify 脚本自检（待补）"
