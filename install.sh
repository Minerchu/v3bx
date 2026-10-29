#!/bin/bash
# V2bX 节点管理面板 —— 安装脚本
# 用法: bash install.sh
set -e

RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[0;33m'; CYN='\033[0;36m'; NC='\033[0m'
info(){ echo -e "${CYN}[*]${NC} $*"; }
ok(){   echo -e "${GRN}[✓]${NC} $*"; }
warn(){ echo -e "${YEL}[!]${NC} $*"; }
die(){  echo -e "${RED}[✗]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && die "请用 root 运行"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/v2bx-panel"
CONF_DIR="/etc/v2bx-panel"
SERVICE="v2bx-panel"

# ---------- 1. 依赖检查 ----------
info "检查运行环境"
command -v python3 >/dev/null 2>&1 || die "缺少 python3"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,7) else 1)' || die "需要 Python 3.7+，当前 $PYV"
ok "Python $PYV（零第三方依赖）"

[[ -f "$SRC_DIR/panel.py" ]] || die "找不到 panel.py，请在解压后的目录里运行"
[[ -f "$SRC_DIR/ui.html" ]] || die "找不到 ui.html"

# ---------- 2. 探测现有配置 ----------
info "探测现有服务与配置"
V2BX_CONFIG="/etc/V2bX/config.json"; V2BX_SERVICE="V2bX"; V2BX_BIN="/usr/local/V2bX/V2bX"
SB_CONFIG="/etc/sing-shadowtls/config.json"; SB_SERVICE="sing-shadowtls"; SB_BIN="/usr/local/bin/sing-box"

if [[ -f /etc/V2bX/config.json ]]; then ok "V2bX 配置: /etc/V2bX/config.json"
else warn "未找到 /etc/V2bX/config.json（面板仍可启动，但节点列表为空）"; fi

for s in V2bX sing-shadowtls; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^${s}.service"; then
    ok "服务存在: $s ($(systemctl is-active $s 2>/dev/null))"
  fi
done

# ---------- 3. 交互配置 ----------
echo
info "配置面板"
if [[ -n "${PANEL_PASSWORD:-}" && ${#PANEL_PASSWORD} -ge 8 ]]; then
  # 非交互模式：环境变量已提供
  PANEL_PORT=${PANEL_PORT:-8080}
  PANEL_BIND=${PANEL_BIND:-0.0.0.0}
  PANEL_USER=${PANEL_USER:-admin}
  ok "使用环境变量配置：${PANEL_USER}@${PANEL_BIND}:${PANEL_PORT}"
else
  read -rp "  监听端口 [8080]: " PANEL_PORT </dev/tty; PANEL_PORT=${PANEL_PORT:-8080}
  read -rp "  监听地址 [0.0.0.0]: " PANEL_BIND </dev/tty; PANEL_BIND=${PANEL_BIND:-0.0.0.0}
  read -rp "  登录用户名 [admin]: " PANEL_USER </dev/tty; PANEL_USER=${PANEL_USER:-admin}
  while :; do
    read -rsp "  登录密码（至少 8 位，留空自动生成）: " PANEL_PASSWORD </dev/tty; echo
    if [[ -z "$PANEL_PASSWORD" ]]; then
      PANEL_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')
      ok "已生成随机密码: ${PANEL_PASSWORD}"
      break
    fi
    [[ ${#PANEL_PASSWORD} -ge 8 ]] && break || warn "密码太短，重新输入"
  done
fi

# 端口占用检查
if ss -tlnp 2>/dev/null | grep -q ":${PANEL_PORT} "; then
  warn "端口 ${PANEL_PORT} 已被占用："
  ss -tlnp | grep ":${PANEL_PORT} "
  if [[ -t 0 ]]; then
    read -rp "  继续安装？[y/N]: " yn
    [[ "$yn" =~ ^[Yy]$ ]] || die "已取消"
  else
    warn "非交互模式，跳过确认"
  fi
fi

# ---------- 4. 安装文件 ----------
info "安装到 ${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR" "$CONF_DIR/backups"
cp "$SRC_DIR/panel.py" "$INSTALL_DIR/panel.py"
cp "$SRC_DIR/ui.html" "$INSTALL_DIR/ui.html"
chmod 755 "$INSTALL_DIR/panel.py"
find "$CONF_DIR" -type d -exec chmod 700 {} \; 2>/dev/null || true
find "$CONF_DIR" -type f -exec chmod 600 {} \; 2>/dev/null || true
ok "文件已就位"

# ---------- 5. 写环境配置 ----------
cat > "$CONF_DIR/panel.env" <<EOF
PANEL_BIND=${PANEL_BIND}
PANEL_PORT=${PANEL_PORT}
PANEL_USER=${PANEL_USER}
PANEL_PASSWORD=${PANEL_PASSWORD}
PANEL_DIR=${CONF_DIR}
V2BX_CONFIG=${V2BX_CONFIG}
V2BX_SERVICE=${V2BX_SERVICE}
V2BX_BIN=${V2BX_BIN}
SINGBOX_CONFIG=${SB_CONFIG}
SINGBOX_SERVICE=${SB_SERVICE}
SINGBOX_BIN=${SB_BIN}
ACME_BIN=/root/.acme.sh/acme.sh
EOF
chmod 600 "$CONF_DIR/panel.env"
ok "环境配置: $CONF_DIR/panel.env"

# ---------- 6. systemd ----------
info "创建 systemd 服务"
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=V2bX Node Management Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${CONF_DIR}/panel.env
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/panel.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

# 加固
NoNewPrivileges=false
PrivateTmp=true
ProtectHome=false

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE} >/dev/null 2>&1
systemctl restart ${SERVICE}
sleep 2

# ---------- 7. 验证 ----------
if systemctl is-active --quiet ${SERVICE}; then
  ok "面板已启动"
else
  die "面板启动失败，查看: journalctl -u ${SERVICE} -n 40 --no-pager"
fi

sleep 1
CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PANEL_PORT}/api/ping" 2>/dev/null || echo "000")
[[ "$CODE" == "200" ]] && ok "HTTP 探活正常" || warn "HTTP 探活返回 $CODE"

# ---------- 8. 输出信息 ----------
IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "========================================================"
echo -e " ${GRN}面板已就绪${NC}"
echo "--------------------------------------------------------"
echo "  地址    : http://${IP}:${PANEL_PORT}/"
echo "  用户名  : ${PANEL_USER}"
echo "  密码    : ${PANEL_PASSWORD}"
echo "  配置    : ${CONF_DIR}/panel.env"
echo "  备份目录: ${CONF_DIR}/backups/"
echo "--------------------------------------------------------"
echo "  常用命令:"
echo "    systemctl status ${SERVICE}"
echo "    journalctl -u ${SERVICE} -f"
echo "    systemctl restart ${SERVICE}"
echo "========================================================"
echo

# 防火墙提示
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  warn "检测到 ufw 已启用，如需外网访问请放行：ufw allow ${PANEL_PORT}/tcp"
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state 2>/dev/null | grep -q running; then
  warn "检测到 firewalld 运行中，放行：firewall-cmd --add-port=${PANEL_PORT}/tcp --permanent && firewall-cmd --reload"
fi

warn "云服务器（阿里云/腾讯云/Oracle/AWS）还需在控制台安全组放行 ${PANEL_PORT}/tcp"
warn "建议：面板端口不要暴露公网，或用 Nginx 加 HTTPS + IP 白名单"
