#!/usr/bin/env bash
#
# v3bx 一键安装 / 升级脚本
#   项目：https://github.com/Minerchu/v3bx
#
# 用法：
#   全新安装：bash <(curl -sL https://raw.githubusercontent.com/Minerchu/v3bx/main/install.sh)
#   仅升级：  bash <(curl -sL https://raw.githubusercontent.com/Minerchu/v3bx/main/install.sh)
#   指定版本：VERSION=v1.0.0 bash <(curl -sL ...)
#
# 非交互安装（不提示，直接写配置）：
#   APIHOST=https://panel.example.com APIKEY=xxxx NODEID=112 NODETYPE=shadowsocks \
#     bash <(curl -sL ...)
#
set -euo pipefail

REPO="Minerchu/v3bx"
BIN_DIR="/usr/local/V2bX"
CONF_DIR="/etc/V2bX"
SVC_NAME="V2bX"
VERSION="${VERSION:-latest}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; BLUE='\033[0;36m'; NC='\033[0m'
info()  { printf "${BLUE}[*]${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}[+]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
die()   { printf "${RED}[x]${NC} %s\n" "$*" >&2; exit 1; }

# ---------- 前置检查 ----------
[ "$(id -u)" -eq 0 ] || die "请用 root 运行"

case "$(uname -m)" in
    x86_64|amd64)   ARCH="amd64" ;;
    aarch64|arm64)  ARCH="arm64" ;;
    *) die "不支持的架构：$(uname -m)（仅提供 amd64 / arm64）" ;;
esac
info "系统架构：$(uname -m) → $ARCH"

command -v curl >/dev/null 2>&1 || die "缺少 curl，请先安装：apt install -y curl / yum install -y curl"
command -v tar  >/dev/null 2>&1 || true

# ---------- 解析下载地址 ----------
if [ "$VERSION" = "latest" ]; then
    DL_URL="https://github.com/${REPO}/releases/latest/download/V2bX-linux-${ARCH}"
    API_URL="https://api.github.com/repos/${REPO}/releases/latest"
else
    DL_URL="https://github.com/${REPO}/releases/download/${VERSION}/V2bX-linux-${ARCH}"
    API_URL="https://api.github.com/repos/${REPO}/releases/tags/${VERSION}"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
TMP_BIN="${TMP_DIR}/V2bX"

info "下载 V2bX (${ARCH})..."
if ! curl -fL --progress-bar --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 300 -o "$TMP_BIN" "$DL_URL"; then
    die "下载失败：$DL_URL
可尝试手动下载后放到 ${BIN_DIR}/V2bX"
fi
[ -s "$TMP_BIN" ] || die "下载的文件为空"

# ---------- 校验 ----------
EXPECTED_SHA=""
if command -v python3 >/dev/null 2>&1; then
    EXPECTED_SHA="$(curl -s --max-time 20 "$API_URL" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
name = 'V2bX-linux-${ARCH}'
for a in d.get('assets', []):
    if a.get('name') == name:
        print((a.get('digest') or '').replace('sha256:', ''))
        break
" 2>/dev/null || true)"
fi

if [ -n "$EXPECTED_SHA" ]; then
    ACTUAL_SHA="$(sha256sum "$TMP_BIN" | awk '{print $1}')"
    if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
        die "校验失败，文件可能损坏
  期望: $EXPECTED_SHA
  实际: $ACTUAL_SHA"
    fi
    ok "SHA256 校验通过"
else
    warn "未能获取官方校验值，跳过校验"
fi

# 确认是 ELF 可执行文件
if command -v file >/dev/null 2>&1; then
    file "$TMP_BIN" | grep -qi 'ELF' || die "下载内容不是可执行文件"
fi
chmod +x "$TMP_BIN"

# ---------- 判断是否已安装 ----------
INSTALLED=0
[ -f "${BIN_DIR}/V2bX" ] && INSTALLED=1

HAS_SYSTEMD=0
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] && HAS_SYSTEMD=1

stop_service() {
    if [ "$HAS_SYSTEMD" -eq 1 ] && systemctl list-unit-files 2>/dev/null | grep -q "^${SVC_NAME}.service"; then
        systemctl stop "$SVC_NAME" 2>/dev/null || true
    fi
}

start_service() {
    if [ "$HAS_SYSTEMD" -eq 1 ]; then
        systemctl daemon-reload
        systemctl enable "$SVC_NAME" >/dev/null 2>&1 || true
        systemctl restart "$SVC_NAME"
        sleep 2
        if systemctl is-active --quiet "$SVC_NAME"; then
            ok "服务已启动：${SVC_NAME}"
        else
            warn "服务启动异常，查看日志：journalctl -u ${SVC_NAME} -n 50 --no-pager"
        fi
    else
        warn "未检测到 systemd，文件已安装但未启动服务"
    fi
}

# ---------- 安装二进制 ----------
mkdir -p "$BIN_DIR"
if [ "$INSTALLED" -eq 1 ]; then
    TS="$(date +%s)"
    cp "${BIN_DIR}/V2bX" "${BIN_DIR}/V2bX.bak.${TS}"
    ok "已备份旧版本：${BIN_DIR}/V2bX.bak.${TS}"
    stop_service
fi
install -m 0755 "$TMP_BIN" "${BIN_DIR}/V2bX"
ok "二进制已安装：${BIN_DIR}/V2bX"

# ---------- 已有配置：直接重启结束 ----------
if [ "$INSTALLED" -eq 1 ] && [ -f "${CONF_DIR}/config.json" ]; then
    start_service
    echo
    ok "升级完成。配置文件保持不变：${CONF_DIR}/config.json"
    echo
    info "面板里该节点若已选 shadow-tls 插件，V2bX 会自动生成外层，无需额外操作。"
    exit 0
fi

# ---------- 全新安装：收集面板信息 ----------
echo
info "首次安装，需要填写面板对接信息（直接回车使用默认值）"

ask() {
    local prompt="$1" default="$2" var=""
    local q
    q="$(printf "${BLUE}[?]${NC} %s" "$prompt")"
    if [ -t 0 ]; then
        read -r -p "$q" var || var=""
    elif { : < /dev/tty; } 2>/dev/null; then
        # curl | bash 的场景：stdin 被占用，改从终端读
        read -r -p "$q" var < /dev/tty || var=""
    fi
    [ -z "$var" ] && var="$default"
    printf '%s' "$var"
}

APIHOST="${APIHOST:-}"
APIKEY="${APIKEY:-}"
NODEID="${NODEID:-}"
NODETYPE="${NODETYPE:-shadowsocks}"
LISTENIP="${LISTENIP:-0.0.0.0}"

if [ -z "$APIHOST" ]; then
    APIHOST="$(ask "面板地址（如 https://panel.example.com）: " "")"
fi
[ -n "$APIHOST" ] || die "面板地址不能为空"

if [ -z "$APIKEY" ]; then
    APIKEY="$(ask "节点通讯密钥（面板里节点的 API Key）: " "")"
fi
[ -n "$APIKEY" ] || die "通讯密钥不能为空"

if [ -z "$NODEID" ]; then
    NODEID="$(ask "节点 ID（面板里节点的数字 ID）: " "")"
fi
[ -n "$NODEID" ] || die "节点 ID 不能为空"

NODETYPE="$(ask "节点类型 [shadowsocks]: " "$NODETYPE")"
LISTENIP="$(ask "监听地址 [0.0.0.0]: " "$LISTENIP")"

mkdir -p "$CONF_DIR"

# 内核基础配置
if [ ! -f "${CONF_DIR}/sing_origin.json" ]; then
    cat > "${CONF_DIR}/sing_origin.json" <<'JSON'
{
  "dns": {
    "servers": [
      { "tag": "cf", "address": "1.1.1.1", "strategy": "ipv4_only" }
    ]
  },
  "outbounds": [
    { "tag": "direct", "type": "direct", "domain_strategy": "ipv4_only" },
    { "type": "block", "tag": "block" }
  ],
  "route": {
    "rules": [
      { "ip_version": 6, "outbound": "block" },
      { "ip_is_private": true, "outbound": "block" }
    ],
    "final": "direct"
  }
}
JSON
    ok "已生成内核配置：${CONF_DIR}/sing_origin.json"
fi

# 节点配置
if [ -f "${CONF_DIR}/config.json" ]; then
    warn "已存在 ${CONF_DIR}/config.json，保留原文件，未覆盖"
    warn "如需添加新节点，请手动编辑该文件"
else
    cat > "${CONF_DIR}/config.json" <<JSON
{
  "Log": { "Level": "info", "Output": "" },
  "Cores": [
    {
      "Type": "sing",
      "Log": { "Level": "info", "Timestamp": true },
      "NTP": { "Enable": false, "Server": "time.apple.com", "ServerPort": 0 },
      "OriginalPath": "${CONF_DIR}/sing_origin.json"
    }
  ],
  "Nodes": [
    {
      "Core": "sing",
      "ApiHost": "${APIHOST}",
      "ApiKey": "${APIKEY}",
      "NodeID": ${NODEID},
      "NodeType": "${NODETYPE}",
      "Timeout": 30,
      "ListenIP": "${LISTENIP}",
      "SendIP": "0.0.0.0",
      "DeviceOnlineMinTraffic": 200,
      "MinReportTraffic": 0,
      "TCPFastOpen": true,
      "SniffEnabled": true
    }
  ]
}
JSON
    chmod 600 "${CONF_DIR}/config.json"
    ok "已生成节点配置：${CONF_DIR}/config.json"
fi

# systemd 单元
if [ "$HAS_SYSTEMD" -eq 1 ] && [ ! -f "/etc/systemd/system/${SVC_NAME}.service" ]; then
    cat > "/etc/systemd/system/${SVC_NAME}.service" <<UNIT
[Unit]
Description=V2bX Service
After=network.target nss-lookup.target

[Service]
Type=simple
User=root
ExecStart=${BIN_DIR}/V2bX server -c ${CONF_DIR}/config.json
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT
    ok "已创建 systemd 服务：${SVC_NAME}"
fi

start_service

echo
ok "安装完成"
echo
printf "  面板地址   %s\n" "$APIHOST"
printf "  节点 ID    %s\n" "$NODEID"
printf "  节点类型   %s\n" "$NODETYPE"
printf "  配置目录   %s\n" "$CONF_DIR"
echo
info "下一步：在面板里把该节点建成 shadowsocks，插件选 shadow-tls，"
info "参数填 host=www.php.net;password=你的密码;version=3"
info "保存后 V2bX 会在 60 秒内自动生成 shadowtls 外层，无需重启。"
echo
info "查看日志：journalctl -u ${SVC_NAME} -f"
