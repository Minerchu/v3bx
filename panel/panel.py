#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2bX / sing-box 节点管理面板
--------------------------------
零依赖（仅 Python 3 标准库），单进程，systemd 托管。

核心保障：任何配置变更都走「备份 -> 临时文件 -> 校验 -> 原子替换 -> 重启 -> 观察 ->
失败自动回滚」的完整事务，绝不把服务改挂。

用法:
    python3 panel.py
    # 或
    PANEL_PORT=8080 PANEL_PASSWORD=xxx python3 panel.py
"""

import os
import re
import ssl
import sys
import json
import time
import socket
import hmac
import base64
import shutil
import secrets
import hashlib
import threading
import subprocess
import urllib.error
import urllib.request
import concurrent.futures
from pathlib import Path
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

# ============================================================
# 配置
# ============================================================

PANEL_DIR      = Path(os.environ.get('PANEL_DIR', '/etc/v2bx-panel'))
PANEL_BIND     = os.environ.get('PANEL_BIND', '0.0.0.0')
PANEL_PORT     = int(os.environ.get('PANEL_PORT', '8080'))
PANEL_USER     = os.environ.get('PANEL_USER', 'admin')
PANEL_PASSWORD = os.environ.get('PANEL_PASSWORD', '')

V2BX_CONFIG    = Path(os.environ.get('V2BX_CONFIG', '/etc/V2bX/config.json'))
V2BX_SERVICE   = os.environ.get('V2BX_SERVICE', 'V2bX')
V2BX_BIN       = os.environ.get('V2BX_BIN', '/usr/local/V2bX/V2bX')

SINGBOX_CONFIG = Path(os.environ.get('SINGBOX_CONFIG', '/etc/sing-shadowtls/config.json'))
SINGBOX_SERVICE = os.environ.get('SINGBOX_SERVICE', 'sing-shadowtls')
SINGBOX_BIN    = os.environ.get('SINGBOX_BIN', '/usr/local/bin/sing-box')

ACME           = os.environ.get('ACME_BIN', '/root/.acme.sh/acme.sh')

BACKUP_DIR     = PANEL_DIR / 'backups'
SESSION_FILE   = PANEL_DIR / 'sessions.json'
AUTH_FILE      = PANEL_DIR / 'auth.json'
CRED_FILE      = PANEL_DIR / 'dns-creds.json'   # 各 DNS 服务商凭据池，跨节点复用
UI_FILE        = Path(__file__).parent / 'ui.html'

MASK = '__KEEP__'          # 敏感字段脱敏哨兵：前端原样回传即代表「不修改」
KEEP_DAYS = 30             # 备份保留天数

# 协议元数据：决定内核可选范围、是否必须 TLS
#
# 这份表必须和 V2bX 源码对齐，否则「面板能选、内核不认」= 服务无限重启：
#   - api/panel/panel.go:59  NewClient 白名单只认
#       vmess / vless / trojan / shadowsocks / hysteria / hysteria2 / tuic / anytls
#     写 socks 或 http 会直接 "unsupported Node type"，节点建不起来。
#   - core/xray/inbound.go:26  xray 只实现 vmess / vless / trojan / shadowsocks
#   - core/sing/node.go:123    sing 覆盖全部 8 种
#
# v2bx=False 表示 V2bX 无法承载，只能给 sing-box 独立实例用。
PROTOCOLS = {
    'vless':       {'label': 'VLESS',        'cores': ['xray', 'sing'], 'tls': 'optional', 'v2bx': True},
    'vmess':       {'label': 'VMess',        'cores': ['xray', 'sing'], 'tls': 'optional', 'v2bx': True},
    'trojan':      {'label': 'Trojan',       'cores': ['xray', 'sing'], 'tls': 'required', 'v2bx': True},
    'shadowsocks': {'label': 'Shadowsocks',  'cores': ['xray', 'sing'], 'tls': 'none',     'v2bx': True},
    'hysteria':    {'label': 'Hysteria (v1)','cores': ['sing'],         'tls': 'required', 'v2bx': True},
    'hysteria2':   {'label': 'Hysteria2',    'cores': ['sing'],         'tls': 'required', 'v2bx': True},
    'tuic':        {'label': 'TUIC',         'cores': ['sing'],         'tls': 'required', 'v2bx': True},
    'anytls':      {'label': 'AnyTLS',       'cores': ['sing'],         'tls': 'required', 'v2bx': True},
    'socks':       {'label': 'SOCKS5',       'cores': ['sing'],         'tls': 'none',     'v2bx': False},
    'http':        {'label': 'HTTP',         'cores': ['sing'],         'tls': 'none',     'v2bx': False},
}

CERT_MODES = {
    'none': '不启用 TLS',
    'file': '使用已有证书文件',
    'http': 'acme.sh HTTP-01（需 80 端口）',
    'dns':  'acme.sh DNS-01（推荐，无需 80 端口）',
    'self': '自签证书',
}

# 面板自己签发/生成证书的落地目录。和 acme.sh 的安装目录分开，
# 便于一眼看出「哪些是面板管的」。
CERT_DIR = Path(os.environ.get('CERT_DIR', '/etc/V2bX/cert'))

DNS_PROVIDERS = ['cloudflare', 'alidns', 'dnspod', 'dns_dp', 'dns_ali', 'gcore', 'route53', 'namecheap']

# 各 provider 需要的环境变量：外层数组是 OR，内层数组是 AND
# 例 cloudflare: (有 DNS_API_TOKEN) 或 (有 API_KEY + EMAIL)
#
# 注意：这里的变量名是 **lego** 的（V2bX 内置 lego 直接读 DNSEnv），
# 不是 acme.sh 的。两者对 Cloudflare 不一样，签发时要转换，见 ACME_ENV_MAP。
PROVIDER_ENV = {
    'cloudflare': [['CLOUDFLARE_DNS_API_TOKEN'], ['CLOUDFLARE_API_KEY', 'CLOUDFLARE_EMAIL']],
    'alidns':     [['Ali_Key', 'Ali_Secret']],
    'dnspod':     [['DP_Id', 'DP_Key']],
    'dns_dp':     [['DP_Id', 'DP_Key']],
    'dns_ali':    [['Ali_Key', 'Ali_Secret']],
    'gcore':      [['GCORE_Key']],
    'route53':    [['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']],
    'namecheap':  [['NAMECHEAP_API_USER', 'NAMECHEAP_API_KEY']],
}

# 界面上的字段文案。键名照搬 lego 的变量名，用户看到的必须是「CF 账号邮箱」
# 而不是 CLOUDFLARE_EMAIL —— 后者既不知道填什么，也不知道和 Token 是二选一。
PROVIDER_FIELDS = {
    'cloudflare': {
        'label': 'Cloudflare',
        'groups': [
            {'title': 'API Token（推荐，只填这一个就行）',
             'keys': ['CLOUDFLARE_DNS_API_TOKEN'],
             'hint': 'CF 后台 → 右上头像 → My Profile → API Tokens → Create Token，'
                     '权限选 <b>Zone → DNS → Edit</b>，Zone Resources 选包含该域名。'
                     '只填这一个就行。'},
            {'title': '或者 Global API Key + 账号邮箱（两个都要填）',
             'keys': ['CLOUDFLARE_API_KEY', 'CLOUDFLARE_EMAIL'],
             'hint': 'My Profile → API Keys → Global API Key。邮箱就是登录 CF 的账号邮箱。'},
        ],
        'fields': {
            'CLOUDFLARE_DNS_API_TOKEN': {'label': 'API Token', 'ph': '粘贴 CF 的 API Token'},
            'CLOUDFLARE_API_KEY':       {'label': 'Global API Key', 'ph': '粘贴 Global API Key'},
            'CLOUDFLARE_EMAIL':         {'label': 'CF 账号邮箱', 'ph': 'you@example.com', 'plain': True},
        },
    },
    'alidns': {
        'label': '阿里云 DNS',
        'groups': [{'title': 'AccessKey（两个都要填）',
                    'keys': ['Ali_Key', 'Ali_Secret'],
                    'hint': '阿里云控制台 → 右上头像 → AccessKey 管理。建议用 RAM 子账号，'
                            '只授权 <b>AliyunDNSFullAccess</b>，别用主账号 AK。'}],
        'fields': {
            'Ali_Key':    {'label': 'AccessKey ID', 'ph': 'LTAI...'},
            'Ali_Secret': {'label': 'AccessKey Secret', 'ph': '点「显示」后复制'},
        },
    },
    'dnspod': {
        'label': 'DNSPod（腾讯云）',
        'groups': [{'title': 'API 密钥（两个都要填）',
                    'keys': ['DP_Id', 'DP_Key'],
                    'hint': 'DNSPod 控制台 → 用户中心 → 安全设置 → API 密钥。'
                            'ID 是纯数字，Token 是一长串。'}],
        'fields': {
            'DP_Id':  {'label': 'DNSPod ID', 'ph': '纯数字'},
            'DP_Key': {'label': 'API Token', 'ph': ''},
        },
    },
    'route53': {
        'label': 'AWS Route 53',
        'groups': [{'title': 'IAM 凭据（两个都要填）',
                    'keys': ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'],
                    'hint': 'IAM 用户需要 <b>route53:ChangeResourceRecordSets</b> 权限。'}],
        'fields': {
            'AWS_ACCESS_KEY_ID':     {'label': 'Access Key ID', 'ph': 'AKIA...'},
            'AWS_SECRET_ACCESS_KEY': {'label': 'Secret Access Key', 'ph': ''},
        },
    },
}
PROVIDER_FIELDS['dns_dp'] = PROVIDER_FIELDS['dnspod']
PROVIDER_FIELDS['dns_ali'] = PROVIDER_FIELDS['alidns']


def provider_fields(prov):
    """给前端的字段定义；没单独配的 provider 退回「变量名当标签」。"""
    d = PROVIDER_FIELDS.get(prov)
    if d:
        return d
    keys = flatten_env_keys(prov)
    return {'label': prov, 'groups': [{'title': '凭据', 'keys': keys, 'hint': ''}],
            'fields': {k: {'label': k, 'ph': ''} for k in keys}}


# acme.sh 的环境变量名和 lego 不一样（Cloudflare 尤其明显）。
# 面板存的是 lego 名（要写进 config.json 给 V2bX 续期用），
# 但调 acme.sh 时必须换成它认的名字，否则 dns_cf 读不到凭据，签发必失败。
ACME_ENV_MAP = {
    'CLOUDFLARE_DNS_API_TOKEN': 'CF_Token',
    'CLOUDFLARE_API_KEY':       'CF_Key',
    'CLOUDFLARE_EMAIL':         'CF_Email',
    'AWS_ACCESS_KEY_ID':        'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY':    'AWS_SECRET_ACCESS_KEY',
}

def flatten_env_keys(prov):
    """把 OR/AND 结构摊平成去重后的字段名列表（供前端渲染输入框）"""
    seen, out = set(), []
    for grp in PROVIDER_ENV.get(prov, []):
        for k in grp:
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out

def check_env(prov, env):
    """返回 (state, detail)：ok / partial / missing"""
    groups = PROVIDER_ENV.get(prov, [])
    if not groups:
        return 'ok', ''
    env = env or {}
    def has(k):
        v = env.get(k)
        return bool(v) and v != MASK
    for grp in groups:
        if all(has(k) for k in grp):
            return 'ok', ''
    have = [k for grp in groups for k in grp if has(k)]
    if have:
        missing = [k for grp in groups for k in grp if not has(k)]
        return 'partial', '缺少 ' + '、'.join(f'<code>{m}</code>' for m in missing)
    return 'missing', ''

# ============================================================
# 工具
# ============================================================

def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def run(cmd, timeout=30, input_text=None):
    """执行命令，返回 (returncode, stdout+stderr)"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, input=input_text)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, f'命令超时（{timeout}s）: {" ".join(cmd)}'
    except FileNotFoundError:
        return 127, f'命令不存在: {cmd[0]}'
    except Exception as e:
        return 1, str(e)

def ensure_dirs():
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PANEL_DIR, 0o700)
    except Exception:
        pass

# ============================================================
# 认证
# ============================================================

def hash_password(pw: str, salt: bytes = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 200_000)
    return {'salt': salt.hex(), 'hash': dk.hex()}

def verify_password(pw: str, rec: dict) -> bool:
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(rec['salt']), 200_000)
    return hmac.compare_digest(dk.hex(), rec['hash'])

class Auth:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}      # token -> {user, exp}
        self.fails = {}         # ip -> [ts...]
        self._load()

    def _load(self):
        try:
            if SESSION_FILE.exists():
                self.sessions = json.loads(SESSION_FILE.read_text())
        except Exception:
            self.sessions = {}

    def _save(self):
        try:
            SESSION_FILE.write_text(json.dumps(self.sessions))
            os.chmod(SESSION_FILE, 0o600)
        except Exception:
            pass

    def bootstrap(self):
        """首次启动：如果没设密码，生成随机密码并落盘提示"""
        if AUTH_FILE.exists():
            return
        pw = PANEL_PASSWORD or secrets.token_urlsafe(12)
        rec = hash_password(pw)
        rec['user'] = PANEL_USER
        AUTH_FILE.write_text(json.dumps(rec))
        os.chmod(AUTH_FILE, 0o600)
        hint = PANEL_DIR / 'INITIAL_PASSWORD.txt'
        hint.write_text(
            f"面板地址: http://<本机IP>:{PANEL_PORT}/\n"
            f"用户名: {PANEL_USER}\n"
            f"密码: {pw}\n"
            f"\n登录后请立即修改密码，并删除本文件。\n"
        )
        os.chmod(hint, 0o600)
        print('=' * 56)
        print('  首次启动，已生成登录凭据')
        print(f'  用户名: {PANEL_USER}')
        print(f'  密码  : {pw}')
        print(f'  已写入: {hint}')
        print('=' * 56, flush=True)

    def check_creds(self, user: str, pw: str, ip: str):
        # 暴力破解防护：同一 IP 5 分钟内失败 10 次锁定
        with self.lock:
            now = time.time()
            hist = [t for t in self.fails.get(ip, []) if now - t < 300]
            if len(hist) >= 10:
                self.fails[ip] = hist
                return False, '尝试次数过多，请 5 分钟后再试'
            try:
                rec = json.loads(AUTH_FILE.read_text())
            except Exception:
                return False, '认证文件损坏'
            if user != rec.get('user') or not verify_password(pw, rec):
                hist.append(now)
                self.fails[ip] = hist
                return False, '用户名或密码错误'
            self.fails.pop(ip, None)
            return True, ''

    def issue(self, user: str) -> str:
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[tok] = {'user': user, 'exp': time.time() + 12 * 3600}
            self._save()
        return tok

    def verify(self, tok: str) -> bool:
        if not tok:
            return False
        with self.lock:
            s = self.sessions.get(tok)
            if not s:
                return False
            if s['exp'] < time.time():
                self.sessions.pop(tok, None)
                self._save()
                return False
            return True

    def revoke(self, tok: str):
        with self.lock:
            self.sessions.pop(tok, None)
            self._save()

    def change_password(self, old: str, new: str):
        rec = json.loads(AUTH_FILE.read_text())
        if not verify_password(old, rec):
            return False, '原密码错误'
        if len(new) < 8:
            return False, '新密码至少 8 位'
        nr = hash_password(new)
        nr['user'] = rec.get('user', 'admin')
        AUTH_FILE.write_text(json.dumps(nr))
        os.chmod(AUTH_FILE, 0o600)
        with self.lock:
            self.sessions.clear()
            self._save()
        return True, ''

# ============================================================
# 服务操作
# ============================================================

def svc_active(name):
    code, out = run(['systemctl', 'is-active', name], timeout=5)
    first = (out or '').strip().splitlines()
    val = first[0].strip() if first else ''
    # 没 systemd 的环境下 systemctl 会把整段错误打进 stderr，这里只认已知状态词
    for word in ('active', 'inactive', 'failed', 'activating', 'deactivating'):
        if val.startswith(word):
            return word
    return 'unknown'

def svc_status(name):
    active = svc_active(name)
    en = run(['systemctl', 'is-enabled', name], timeout=5)[1].strip().splitlines()
    enabled = en[0].strip() if en and en[0].strip() in (
        'enabled', 'disabled', 'static', 'masked', 'indirect') else 'unknown'
    code, out = run(['systemctl', 'show', name, '-p', 'ActiveEnterTimestamp', '-p', 'NRestarts'], timeout=5)
    info = {}
    for line in out.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            info[k] = v
    since = info.get('ActiveEnterTimestamp', '').strip()
    return {'name': name, 'active': active, 'enabled': enabled,
            'since': since[:19],
            'restarts': (info.get('NRestarts', '0').strip() or '0')[:8]}

def svc_restart(name):
    return run(['systemctl', 'restart', name], timeout=40)


def sync_port_hop():
    """端口跳跃（mport）规则的目标端口，跟着 V2bX 实际监听的端口走。

    踩过的坑：用户在 Xboard 把 hy2 端口从 443 改成 2500，V2bX 也监听 2500 了，
    但 `udp 50000-51000 REDIRECT --to-ports 443` 这条老规则还指着 443。
    客户端订阅里带 mport 时，包全被转到没人监听的 443，表现就是
    「hy2 连不上，但同一台机器的 ss 正常、别的机器的 hy2 也正常」——
    因为 ss 走 TCP、别的 hy2 在别的机器上。

    只在「目标端口确实没人监听」时才改，用户自定义的其他 REDIRECT 不动。
    返回改动列表（空列表表示不需要改）。
    """
    code, out = run(['iptables', '-t', 'nat', '-S', 'PREROUTING'], timeout=6)
    if code != 0 or 'REDIRECT' not in out:
        return []

    live, v2bx_udp = set(), set()
    code2, ssout = run(['ss', '-ulnp'], timeout=6)
    if code2 == 0:
        for line in ssout.splitlines():
            m = re.search(r':(\d{1,5})\s', line)
            if not m:
                continue
            port = int(m.group(1))
            live.add(port)
            if 'V2bX' in line:
                v2bx_udp.add(port)
    if not v2bx_udp:
        return []

    newport = sorted(v2bx_udp)[0]
    fixes = []
    for line in out.splitlines():
        if 'REDIRECT' not in line or '--dport' not in line:
            continue
        mt = re.search(r'--to-ports\s+(\d+)', line)
        if not mt:
            continue
        target = int(mt.group(1))
        if target in live or target == newport:
            continue          # 目标端口有服务在听，或已经是对的，不动
        old_args = line[3:].split()
        new_args = line.replace(f'--to-ports {target}',
                                f'--to-ports {newport}')[3:].split()
        c1, _ = run(['iptables', '-t', 'nat', '-D'] + old_args, timeout=6)
        if c1 != 0:
            continue
        c2, _ = run(['iptables', '-t', 'nat', '-A'] + new_args, timeout=6)
        if c2 == 0:
            fixes.append(f'{target} → {newport}')
    if fixes:
        # 存盘，否则重启机器后规则又回到旧端口
        run(['netfilter-persistent', 'save'], timeout=15)
    return fixes

def singbox_installed():
    """这台机器上有没有独立的 sing-box 实例（shadowtls + ss + hy2 那一套）。

    新装的机器只有 V2bX 一个服务，/etc/sing-shadowtls/ 根本不存在。
    之前保存时不加判断地去写那个路径、重启那个服务，结果：
      - 写配置时父目录不存在 -> FileNotFoundError -> 接口 500
      - 重启不存在的服务 -> wait_stable 判失败 -> 触发回滚
    两种表现都是「保存不了」。
    """
    if SINGBOX_CONFIG.exists():
        return True
    code, _ = run(['systemctl', 'cat', SINGBOX_SERVICE], timeout=5)
    return code == 0

def get_logs(name, lines=120):
    code, out = run(['journalctl', '-u', name, '-n', str(lines),
                     '--no-pager', '-o', 'cat'], timeout=15)
    return out if code == 0 else f'读取日志失败: {out}'

def extract_errors(log_text):
    """从日志中提取错误行"""
    pats = re.compile(r'(level=error|level=fatal|panic:|FATAL|unknown core type|'
                      r'failed to|cannot |invalid |no such |address already in use|'
                      r'authentication failed|error:)', re.I)
    out = []
    for line in log_text.splitlines():
        if pats.search(line):
            out.append(line.strip())
    return out[-40:]

def wait_stable(name, settle=2.0, confirm=4.0):
    """
    判断服务是否真的稳住了。
    V2bX 崩溃时会处于 activating (auto-restart)，is-active 返回 activating。
    先等 settle 秒看是否 active，再等 confirm 秒确认没有二次崩溃。
    """
    time.sleep(settle)
    if svc_active(name) != 'active':
        return False
    time.sleep(confirm)
    return svc_active(name) == 'active'

def listen_ports():
    """返回 {(proto, port): process}，只保留真正的「监听」端口。

    两个坑：
    1. 列数：Netid State Recv-Q Send-Q Local Peer Process，地址在第 5 列。
       原来只跳 3 列，把 Send-Q 的 "0" 当成了地址，端口列表永远为空。
    2. `ss -l` 对 UDP 也会列出「已连接」的临时 socket（peer 是具体地址），
       这些不是监听端口，混进来会让界面显示一堆随机高位端口。
    """
    code, out = run(['ss', '-tulnp'], timeout=10)
    res = {}
    for line in out.splitlines()[1:]:
        m = re.search(r'^(tcp|udp)\s+(\S+)\s+\S+\s+\S+\s+(\S+)\s+(\S+)', line)
        if not m:
            continue
        proto, state, addr, peer = m.group(1), m.group(2), m.group(3), m.group(4)
        if proto == 'tcp' and state != 'LISTEN':
            continue
        if proto == 'udp' and not peer.endswith(':*'):
            continue          # 已连接的 UDP socket，不是监听
        pm = re.search(r':(\d+)$', addr)
        if not pm:
            continue
        pm2 = re.search(r'users:\(\("([^"]+)"', line)
        res[(proto, int(pm.group(1)))] = pm2.group(1) if pm2 else ''
    return res

# ============================================================
# IPv6 能力检测
# ============================================================
#
# 为什么需要：V2bX 默认把节点监听在 `::`，出站（freedom / sing direct）交给
# 系统解析器时，DNS 返回 AAAA 就先试 IPv6。机器根本没有 IPv6 出口时，每条
# 连接都要先吃一个 "network is unreachable"（实测 0.25~1.5s）才回落 IPv4，
# 症状就是「能连但很卡 / 时断时续」。
#
# 所以先真实探一次能不能出网：
#   - 能  -> 保持双栈（prefer_ipv4，v6 可用但排在 v4 后面，避免 v6 路由质量差时更慢）
#   - 不能 -> 域名解析钉死 ipv4_only，监听地址从 `::` 退回 `0.0.0.0`
#            （内核禁用 IPv6 模块时 bind `::` 会直接失败，节点起不来）

IPV6_FILE    = PANEL_DIR / 'ipv6.json'
IPV6_TTL     = 300
IPV6_TARGETS = [('2606:4700:4700::1111', 443),   # Cloudflare
                ('2001:4860:4860::8888', 443),   # Google
                ('2620:fe::fe', 443)]            # Quad9
_ipv6_cache  = {'ts': 0.0, 'res': None}


def ipv6_addrs():
    """本机可用于出网的 IPv6 地址。

    只认全球单播 2000::/3：
      - fe80::  链路本地，出不了网
      - fc00::/7  ULA（相当于 IPv4 的 192.168.x），同样出不了网
    实测有机器只挂了个 fd00:dead:beef::1 这种，看起来「有 v6」其实没有，
    列表里摆出来只会误导。
    """
    out = []
    try:
        text = Path('/proc/net/if_inet6').read_text()
    except Exception:
        return out
    for line in text.splitlines():
        f = line.split()
        if len(f) < 6:
            continue
        hexaddr, scope, name = f[0], f[3], f[5]
        if name == 'lo' or scope == '20':        # 0x20 = link-local
            continue
        if hexaddr[0] not in '23':               # 2000::/3
            continue
        try:
            out.append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(hexaddr)))
        except Exception:
            pass
    return out


def _v6_connect(host, port, timeout=2.0):
    s = None
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        return True, ''
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def ipv6_probe():
    """真实探测：有全局地址 + 能连出去，才算「有 IPv6」。"""
    res = {'ok': False, 'addrs': ipv6_addrs(), 'route': False,
           'target': '', 'detail': '', 'ts': int(time.time())}
    if not res['addrs']:
        res['detail'] = '本机没有全局 IPv6 地址'
        return res

    code, out = run(['ip', '-6', 'route', 'show', 'default'], timeout=5)
    res['route'] = bool(code == 0 and (out or '').strip())

    last_err = ''
    for host, port in IPV6_TARGETS:
        ok, err = _v6_connect(host, port)
        if ok:
            res.update(ok=True, target=f'[{host}]:{port}',
                       detail=f'可出网（{host}:{port} 握手成功）')
            return res
        last_err = err

    hint = '有默认路由但外连失败' if res['route'] else '没有 IPv6 默认路由'
    res['detail'] = f'{hint}：{last_err or "全部目标不可达"}'
    return res


def ipv6_mode():
    try:
        m = (json.loads(IPV6_FILE.read_text()) or {}).get('mode', 'auto')
    except Exception:
        m = 'auto'
    return m if m in ('auto', 'on', 'off') else 'auto'


def set_ipv6_mode(mode):
    if mode not in ('auto', 'on', 'off'):
        raise ValueError('mode 只能是 auto / on / off')
    ensure_dirs()
    tmp = IPV6_FILE.with_name('ipv6.json.tmp')
    tmp.write_text(json.dumps({'mode': mode}, indent=2))
    os.replace(tmp, IPV6_FILE)
    return ipv6_state()


def ipv6_state(force=False):
    now = time.time()
    if force or _ipv6_cache['res'] is None or now - _ipv6_cache['ts'] > IPV6_TTL:
        _ipv6_cache['res'] = ipv6_probe()
        _ipv6_cache['ts'] = now
    det = dict(_ipv6_cache['res'])
    mode = ipv6_mode()
    if mode == 'on':
        enabled, why = True, '手动启用'
    elif mode == 'off':
        enabled, why = False, '手动关闭'
    else:
        enabled, why = bool(det['ok']), '自动检测'
    return {'mode': mode, 'enabled': enabled, 'why': why,
            'detected': det, 'age': int(now - _ipv6_cache['ts'])}


def ipv6_enabled():
    """配置渲染时用这个，只取布尔值。"""
    try:
        return bool(ipv6_state()['enabled'])
    except Exception:
        return True     # 探测炸了就按「有」处理，别把 IPv6 用户误伤成单栈


SING_ORIGIN = Path(os.environ.get('SING_ORIGIN', '/etc/V2bX/sing_origin.json'))


def _sing_origin_skeleton(ds):
    """最小可用模板：一条直连、一条阻断，私有地址不往外走。
    不带任何去广告/正则规则——那些是用户自己的策略，面板不该凭空塞。"""
    return {
        'dns': {'servers': [{'tag': 'cf', 'address': '1.1.1.1', 'strategy': ds}]},
        'outbounds': [{'tag': 'direct', 'type': 'direct', 'domain_strategy': ds},
                      {'tag': 'block', 'type': 'block'}],
        'route': {'rules': [{'ip_is_private': True, 'outbound': 'block'},
                            {'outbound': 'direct', 'network': ['udp', 'tcp']}]},
        'experimental': {'cache_file': {'enabled': True}},
    }


def ensure_sing_origin(v6, nodes=None):
    """V2bX 的 sing 内核会读 OriginalPath，文件不存在就直接启动失败
    （core/sing/sing.go:55 `read original config error`）。
    安装脚本以前 cp 的是 example/sing_origin.json，而仓库里根本没这个文件，
    新机首装必然踩空。这里兜底创建，并顺手把出站/解析的域名策略钉死。"""
    ds = 'prefer_ipv4' if v6 else 'ipv4_only'
    existed = SING_ORIGIN.exists()
    cfg = None
    if existed:
        try:
            cfg = json.loads(SING_ORIGIN.read_text())
        except Exception:
            cfg = None

    created = False
    if not isinstance(cfg, dict) or not cfg:
        # 文件缺失 / 内容坏了：先给一份完整骨架，再往下走（要给 nodes 追加 socks 出站）
        cfg = _sing_origin_skeleton(ds)
        created = True

    changed = False
    obs = cfg.get('outbounds')
    if not isinstance(obs, list) or not obs:
        cfg['outbounds'] = _sing_origin_skeleton(ds)['outbounds']
        changed = True
    else:
        if not any(isinstance(o, dict) and o.get('tag') == 'direct' for o in obs):
            obs.append({'type': 'direct', 'tag': 'direct', 'domain_strategy': ds})
            changed = True
        for o in obs:
            if isinstance(o, dict) and o.get('type') == 'direct' and o.get('domain_strategy') != ds:
                o['domain_strategy'] = ds
                changed = True

    dns = cfg.get('dns')
    if isinstance(dns, dict) and isinstance(dns.get('servers'), list):
        for s in dns['servers']:
            if isinstance(s, dict) and s.get('strategy') != ds:
                s['strategy'] = ds
                changed = True

    # ---- 每节点 socks5 出口：写入 sing-box 的 outbound + route 规则 ----
    if nodes is not None:
        if not isinstance(cfg.get('outbounds'), list):
            cfg['outbounds'] = _sing_origin_skeleton(ds)['outbounds']
        route = cfg.get('route')
        if not isinstance(route, dict):
            route = cfg['route'] = {}
        if not isinstance(route.get('rules'), list):
            route['rules'] = []
        # 清掉上一轮面板托管的出站/规则（tag 前缀 ob-node-），保证可重复执行
        cfg['outbounds'] = [o for o in cfg['outbounds']
                            if not (isinstance(o, dict) and str(o.get('tag', '')).startswith('ob-node-'))]
        route['rules'] = [r for r in route['rules']
                          if not (isinstance(r, dict) and str(r.get('outbound', '')).startswith('ob-node-'))]
        # 私有地址拦截必须留在最前面：节点出口是 socks 时也不能放内网流量出去
        head, rest = [], []
        for r in route['rules']:
            if isinstance(r, dict) and r.get('ip_is_private') and not r.get('inbound'):
                head.append(r)
            else:
                rest.append(r)
        new_rules = []
        for n in nodes:
            if n.get('owner') != 'v2bx' or n.get('core') != 'sing':
                continue
            oc = n.get('outbound') or {}
            if oc.get('type') != 'socks5':
                continue
            s5 = oc.get('socks5') or {}
            server = (s5.get('server') or '').strip()
            if not server:
                continue
            try:
                nid = int(n.get('nodeId') or 0)
                port = int(s5.get('port') or 1080)
            except Exception:
                continue
            if nid <= 0 or port < 1 or port > 65535:
                continue
            in_tag = f'node-{nid}'
            ob_tag = f'ob-node-{nid}'
            ob = {'type': 'socks', 'tag': ob_tag, 'server': server,
                  'server_port': port, 'version': '5'}
            if s5.get('username'):
                ob['username'] = s5.get('username')
            if s5.get('password'):
                ob['password'] = s5.get('password')
            cfg['outbounds'].append(ob)
            new_rules.append({'inbound': [in_tag], 'outbound': ob_tag})
        route['rules'] = head + new_rules + rest

    # 幂等：内容真变了才写盘、才报「有改动」
    text = json.dumps(cfg, indent=2, ensure_ascii=False)
    old_text = None
    if existed:
        try:
            old_text = SING_ORIGIN.read_text()
        except Exception:
            old_text = None
    if created or text != old_text:
        SING_ORIGIN.parent.mkdir(parents=True, exist_ok=True)
        SING_ORIGIN.write_text(text)
        return True
    return False

# ============================================================
# DNS 凭据池
# ============================================================

def load_creds():
    try:
        return json.loads(CRED_FILE.read_text())
    except Exception:
        return {}

def save_creds(creds):
    try:
        CRED_FILE.write_text(json.dumps(creds, ensure_ascii=False, indent=2))
        os.chmod(CRED_FILE, 0o600)
    except Exception:
        pass

def merge_creds(nodes):
    """把本次提交里填了真实值的凭据并进池子"""
    creds = load_creds()
    changed = False
    for n in nodes:
        c = n.get('cert') or {}
        if c.get('mode') != 'dns':
            continue
        prov = c.get('provider') or 'cloudflare'
        for k, v in (c.get('dnsEnv') or {}).items():
            if v and v != MASK and creds.get(prov, {}).get(k) != v:
                creds.setdefault(prov, {})[k] = v
                changed = True
    if changed:
        save_creds(creds)

def resolve_creds(node):
    """把节点里 __KEEP__ / 缺失的凭据，用池子里的真实值补齐（原地修改）"""
    c = node.get('cert') or {}
    if c.get('mode') != 'dns':
        return
    prov = c.get('provider') or 'cloudflare'
    pool = load_creds().get(prov, {})
    if not pool:
        return
    env = c.setdefault('dnsEnv', {})
    for k, v in pool.items():
        if not env.get(k) or env.get(k) == MASK:
            env[k] = v

def masked_creds():
    """给前端的凭据形状（值全部脱敏，只用于展示和继承）"""
    return {p: {k: '••••••••' for k in v} for p, v in load_creds().items()}

# ============================================================
# 备份
# ============================================================

def backup_file(path: Path, tag=''):
    ensure_dirs()
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    name = f'{path.name}.{tag + "." if tag else ""}{ts}.bak'
    dst = BACKUP_DIR / name
    shutil.copy2(path, dst)
    # 清理过期
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in BACKUP_DIR.glob('*.bak'):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass
    return dst

def list_backups(limit=40):
    ensure_dirs()
    items = []
    for f in sorted(BACKUP_DIR.glob('*.bak'), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        items.append({'name': f.name, 'size': f.stat().st_size,
                      'time': datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')})
    return items

# ============================================================
# 节点模型
# ============================================================

def blank_node(owner='v2bx', node_type='vless'):
    meta = PROTOCOLS.get(node_type, PROTOCOLS['vless'])
    # 证书默认值：
    #   tls=optional（vless/vmess）-> 不启用。这两个协议本来就能明文跑，
    #     默认塞一张自签只会逼用户去填一个他还没想好的域名。
    #   tls=required（trojan/hy2/tuic）-> 自签。自签不需要任何凭据，点保存
    #     就能起；想换真证书改一下下拉即可。原来默认 dns，新用户没有 API
    #     凭据时会卡在「缺少 API 凭据」上，而他根本不知道要去哪申请。
    return {
        'uid': secrets.token_hex(6),
        'owner': owner,
        'nodeId': 1,
        'nodeType': node_type,
        'core': meta['cores'][0],
        'listenIP': '::' if owner == 'v2bx' else '0.0.0.0',
        'listenPort': 443 if owner == 'singbox' else 0,
        'cert': {
            'mode': 'none' if meta['tls'] in ('none', 'optional') else 'self',
            'domain': '',
            'certFile': '',
            'keyFile': '',
            'email': '',
            'provider': 'cloudflare',
            'dnsEnv': {},
        },
        'outbound': {
            'type': 'direct',
            'socks5': {'server': '', 'port': 1080, 'username': '', 'password': ''},
        },
        'remark': '',
        # sing-box 专用
        'transport': 'ws',
        'path': '/' + secrets.token_hex(4),
        'host': '',
        'method': '2022-blake3-aes-256-gcm',
        'password': '',
        'upMbps': 100,
        'downMbps': 500,
        'obfs': '',
        'obfsPassword': '',
        'masquerade': 'https://www.bing.com',
        'congestionControl': 'bbr',
        'stlsDetourPort': 0,
        'stlsHandshake': 'www.php.net',
        'stlsHandshakeDetour': 'direct',
        'stlsPassword': '',
    }

def is_template_node(nd):
    """安装包自带的示例节点。

    上游的 config.json 模板里写了一个 NodeID 33 / NodeType shadowsocks /
    ApiHost http://127.0.0.1 / ApiKey test / 自签 example.com 的假节点。
    老版本 install.sh 直接把这个模板拷进 /etc/V2bX/，于是面板一打开就
    多出一个"默认节点"。三个特征同时命中才判定，避免误伤真实配置。
    """
    host = (nd.get('ApiHost') or '').strip().lower()
    return (
        nd.get('NodeID') == 33
        and (nd.get('ApiKey') or '') == 'test'
        and ('127.0.0.1' in host or 'localhost' in host)
    )


def parse_v2bx_nodes(cfg):
    nodes = []
    for nd in cfg.get('Nodes', []):
        if is_template_node(nd):
            continue
        nt = nd.get('NodeType', 'vless')
        n = blank_node('v2bx', nt)
        n['nodeId'] = nd.get('NodeID', 0)
        n['core'] = nd.get('Core', 'sing')
        n['listenIP'] = nd.get('ListenIP', '::')
        cc = nd.get('CertConfig') or {}
        n['cert'] = {
            'mode': (cc.get('CertMode') or 'none').lower(),
            'domain': cc.get('CertDomain', ''),
            'certFile': cc.get('CertFile', ''),
            'keyFile': cc.get('KeyFile', ''),
            'email': cc.get('Email', ''),
            'provider': cc.get('Provider', 'cloudflare'),
            'dnsEnv': dict(cc.get('DNSEnv') or {}),
        }
        oc = nd.get('OutboundConfig') or {}
        s5 = oc.get('Socks5') or {}
        n['outbound'] = {
            'type': (oc.get('Type') or 'direct').lower(),
            'socks5': {
                'server': s5.get('Server', '') or '',
                'port': int(s5.get('Port') or 1080),
                'username': s5.get('Username', '') or '',
                'password': s5.get('Password', '') or '',
            },
        }
        n['remark'] = cc.get('CertDomain', '') or f'Node {n["nodeId"]}'
        nodes.append(n)
    return nodes

def parse_singbox_nodes(cfg):
    nodes = []
    inbounds = cfg.get('inbounds', [])
    by_tag = {ib.get('tag'): ib for ib in inbounds}
    for ib in inbounds:
        t = ib.get('type')
        if t == 'shadowtls':
            # shadowtls 是外层，它 detour 指向内层 ss，合并成一个节点卡片
            inner = by_tag.get(ib.get('detour')) or {}
            n = blank_node('singbox', 'shadowsocks')
            n['nodeType'] = 'shadowsocks'
            n['listenIP'] = ib.get('listen', '0.0.0.0')
            n['listenPort'] = ib.get('listen_port', 0)
            n['stlsDetourPort'] = inner.get('listen_port', 0)
            hs = ib.get('handshake') or {}
            n['stlsHandshake'] = hs.get('server', '')
            n['stlsHandshakeDetour'] = hs.get('detour', 'direct')
            n['stlsPassword'] = (ib.get('users') or [{}])[0].get('password', '') if ib.get('users') else ''
            n['method'] = inner.get('method', '2022-blake3-aes-256-gcm')
            n['password'] = inner.get('password', '')
            n['remark'] = f'shadowtls → ss :{n["listenPort"]}'
            n['_tags'] = [ib.get('tag'), inner.get('tag')]
            n['stlsTag'] = ib.get('tag')
            n['ssTag'] = inner.get('tag')
            nodes.append(n)
            continue
        if t in ('shadowsocks', 'socks', 'http') and ib.get('listen', '').startswith('127.0.0.1'):
            continue  # 已被 shadowtls 合并，跳过
        if t not in PROTOCOLS:
            continue
        n = blank_node('singbox', t)
        n['listenIP'] = ib.get('listen', '0.0.0.0')
        n['listenPort'] = ib.get('listen_port', 0)
        n['_tag'] = ib.get('tag')
        tls = ib.get('tls') or {}
        if tls.get('enabled'):
            n['cert']['mode'] = 'file'
            n['cert']['domain'] = tls.get('server_name', '')
            n['cert']['certFile'] = tls.get('certificate_path', '')
            n['cert']['keyFile'] = tls.get('key_path', '')
        else:
            n['cert']['mode'] = 'none'
        if t == 'hysteria2':
            n['masquerade'] = ib.get('masquerade', '')
            n['obfs'] = (ib.get('obfs') or {}).get('type', '')
            n['obfsPassword'] = (ib.get('obfs') or {}).get('password', '')
        if t == 'tuic':
            n['congestionControl'] = ib.get('congestion_control', 'bbr')
        if t == 'shadowsocks':
            n['method'] = ib.get('method', '')
            n['password'] = ib.get('password', '')
        n['remark'] = f'{t} :{n["listenPort"]}'
        nodes.append(n)
    return nodes

def render_v2bx_node(n, g, ipv6=True):
    lip = n.get('listenIP') or '::'
    if not ipv6 and lip in ('::', '[::]'):
        # 系统禁用 IPv6 时 bind `::` 直接 EAFNOSUPPORT，节点起不来
        lip = '0.0.0.0'
    o = {
        'Core': n['core'],
        'ApiHost': g.get('apiHost', ''),
        'ApiKey': g.get('apiKey', ''),
        'NodeID': int(n['nodeId']),
        'NodeType': n['nodeType'],
        'Timeout': 30,
        'ListenIP': lip,
        'SendIP': '0.0.0.0',
        'DeviceOnlineMinTraffic': 200,
        'MinReportTraffic': 0,
        'TCPFastOpen': True,
        'SniffEnabled': True,
    }
    # 出站域名策略：
    #   xray —— freedom 的 DomainStrategy 只在 EnableDNS=true 时才生效
    #           （core/xray/outbound.go:26），UseIPv4 = 只解析 A 记录
    #   sing —— 真正的开关在 sing_origin.json 的 outbound/dns，见 ensure_sing_origin()；
    #           这里写一份同值，方便别人看配置就知道意图
    if n['core'] == 'xray':
        o['XrayOptions'] = {'EnableDNS': True,
                            'DNSType': 'UseIP' if ipv6 else 'UseIPv4'}
    else:
        o['SingOptions'] = {'EnableDNS': True,
                            'DomainStrategy': 'prefer_ipv4' if ipv6 else 'ipv4_only'}
    c = n.get('cert') or {}
    if c.get('mode') and c['mode'] != 'none':
        o['CertConfig'] = {
            'CertMode': c['mode'],
            'RejectUnknownSni': False,
            'CertDomain': c.get('domain', ''),
            'CertFile': c.get('certFile', ''),
            'KeyFile': c.get('keyFile', ''),
            'Email': c.get('email', ''),
            'Provider': c.get('provider', 'cloudflare'),
            'DNSEnv': c.get('dnsEnv') or {},
        }
    # 每节点出口：本地直连 or socks5
    oc = n.get('outbound') or {}
    if oc.get('type') == 'socks5' and (oc.get('socks5') or {}).get('server'):
        s5 = oc.get('socks5') or {}
        o['OutboundConfig'] = {
            'Type': 'socks5',
            'Socks5': {
                'Server': s5.get('server', ''),
                'Port': int(s5.get('port') or 1080),
                'Username': s5.get('username', '') or '',
                'Password': s5.get('password', '') or '',
            },
        }
    else:
        o['OutboundConfig'] = {'Type': 'direct'}
    try:
        o['Name'] = f'node-{int(n.get("nodeId") or 0)}'
    except Exception:
        pass
    return o

def render_singbox_inbounds(n, ipv6=True):
    """一个节点 -> 一个或多个 inbound"""
    t = n['nodeType']
    port = int(n.get('listenPort') or 0)
    ip = n.get('listenIP') or '0.0.0.0'
    if not ipv6 and ip in ('::', '[::]'):
        ip = '0.0.0.0'
    tls = None
    c = n.get('cert') or {}
    if c.get('mode') and c['mode'] != 'none':
        tls = {
            'enabled': True,
            'server_name': c.get('domain', ''),
            'certificate_path': c.get('certFile', ''),
            'key_path': c.get('keyFile', ''),
        }

    if t == 'shadowsocks' and n.get('stlsDetourPort'):
        # 生成 shadowtls 外层 + ss 内层
        outer_port = port
        inner_port = int(n['stlsDetourPort'])
        tag_out = n.get('stlsTag') or f'stls-{outer_port}'
        tag_in = n.get('ssTag') or f'ss-{inner_port}'
        hs_detour = n.get('stlsHandshakeDetour') or 'direct'
        outer = {
            'type': 'shadowtls',
            'tag': tag_out,
            'listen': ip,
            'listen_port': outer_port,
            'version': 3,
            'users': [{'name': 'u1', 'password': n.get('stlsPassword') or secrets.token_urlsafe(12)}],
            'handshake': {'server': n.get('stlsHandshake') or 'www.php.net', 'server_port': 443,
                          'detour': hs_detour},
            'detour': tag_in,
        }
        inner = {
            'type': 'shadowsocks',
            'tag': tag_in,
            'listen': '127.0.0.1',
            'listen_port': inner_port,
            'method': n.get('method') or '2022-blake3-aes-256-gcm',
            'password': n.get('password') or base64.b64encode(secrets.token_bytes(32)).decode(),
            'users': [],
        }
        return [outer, inner]

    ib = {
        'type': t,
        'tag': n.get('_tag') or f'{t}-{port}',
        'listen': ip,
        'listen_port': port,
    }
    if t == 'shadowsocks':
        ib['method'] = n.get('method') or '2022-blake3-aes-256-gcm'
        ib['password'] = n.get('password') or base64.b64encode(secrets.token_bytes(32)).decode()
        ib['users'] = []
    elif t in ('vless', 'vmess', 'trojan'):
        ib['users'] = []
        # 传输层：之前这里完全没写，UI 上选了 ws/grpc 保存后配置里啥也没有，
        # 用户以为生效了其实还是裸 tcp。
        tr = (n.get('transport') or 'tcp').lower()
        if tr in ('ws', 'grpc', 'httpupgrade', 'http'):
            path = n.get('path') or '/'
            host = (n.get('host') or '').strip()
            if tr == 'ws':
                ib['transport'] = {'type': 'ws', 'path': path}
                if host:
                    ib['transport']['headers'] = {'Host': host}
            elif tr == 'grpc':
                ib['transport'] = {'type': 'grpc',
                                   'service_name': path.strip('/') or 'grpc'}
            else:
                ib['transport'] = {'type': 'httpupgrade', 'path': path}
                if host:
                    ib['transport']['host'] = host
    elif t == 'hysteria2':
        ib['users'] = []
        if n.get('obfs'):
            ib['obfs'] = {'type': n['obfs'], 'password': n.get('obfsPassword', '')}
        if n.get('masquerade'):
            ib['masquerade'] = n['masquerade']
        # Brutal 拥塞控制靠这两个值；不写就退化成 BBR
        if n.get('upMbps'):
            ib['up_mbps'] = int(n['upMbps'])
        if n.get('downMbps'):
            ib['down_mbps'] = int(n['downMbps'])
    elif t == 'tuic':
        ib['users'] = []
        ib['congestion_control'] = n.get('congestionControl', 'bbr')
    elif t == 'anytls':
        ib['users'] = []
    if tls:
        ib['tls'] = tls
    return [ib]

# ============================================================
# 校验
# ============================================================

def validate_node(n, all_nodes, g):
    """返回 [{level, field, msg}]"""
    out = []
    normalize_cert(n)
    t = n.get('nodeType')
    meta = PROTOCOLS.get(t)
    if not meta:
        out.append({'level': 'error', 'field': 'nodeType', 'msg': f'未知协议 {t}'})
        return out

    # V2bX 的 NewClient 有一层白名单（api/panel/panel.go:59），不在名单里的
    # NodeType 会让节点建不起来、服务无限重启。socks / http 只能给独立
    # sing-box 实例用。
    if n.get('owner') == 'v2bx' and not meta.get('v2bx', True):
        out.append({'level': 'error', 'field': 'nodeType',
                    'msg': f'V2bX 不支持 <code>{t}</code> 协议。它的 NodeType 白名单只有 '
                           f'vmess / vless / trojan / shadowsocks / hysteria / hysteria2 / tuic / anytls。'
                           f'要用 {meta["label"]} 请把承载进程改成 sing-box'})

    # 内核：V2bX 的 Cores[].Type 只认 xray / sing，写别的会导致
    # "unknown core type: xxx" 然后无限重启（三个节点一起挂）
    core = n.get('core')
    if core not in ('xray', 'sing'):
        out.append({'level': 'error', 'field': 'core',
                    'msg': f'内核 <code>{core}</code> 不合法。V2bX 的 <code>Cores[].Type</code> '
                           f'只认 <code>xray</code> / <code>sing</code>，写其他值会让服务无限重启'})
    elif core not in meta['cores']:
        out.append({'level': 'error', 'field': 'core',
                    'msg': f'{meta["label"]} 不支持 <code>{core}</code> 内核，请改成 '
                           + ' / '.join(f'<code>{c}</code>' for c in meta['cores'])})

    # NodeID
    if n.get('owner') == 'v2bx':
        try:
            nid = int(n.get('nodeId') or 0)
            if nid <= 0:
                out.append({'level': 'error', 'field': 'nodeId', 'msg': 'NodeID 必须是正整数'})
            elif any(int(x.get('nodeId') or 0) == nid and x.get('uid') != n.get('uid')
                     for x in all_nodes if x.get('owner') == 'v2bx'):
                out.append({'level': 'error', 'field': 'nodeId',
                            'msg': f'NodeID <code>{nid}</code> 已被另一个节点占用'})
        except Exception:
            out.append({'level': 'error', 'field': 'nodeId', 'msg': 'NodeID 必须是数字'})

    # 端口
    if n.get('owner') == 'singbox':
        try:
            port = int(n.get('listenPort') or 0)
            if port < 1 or port > 65535:
                out.append({'level': 'error', 'field': 'listenPort',
                            'msg': f'端口 <code>{port}</code> 非法（1-65535）'})
            else:
                for other in all_nodes:
                    if other.get('uid') == n.get('uid'):
                        continue
                    op = int(other.get('listenPort') or 0)
                    if op == port:
                        out.append({'level': 'error', 'field': 'listenPort',
                                    'msg': f'端口 <code>{port}</code> 已被「{other.get("remark") or other.get("nodeType")}」占用'})
                    od = int(other.get('stlsDetourPort') or 0)
                    if od == port:
                        out.append({'level': 'error', 'field': 'listenPort',
                                    'msg': f'端口 <code>{port}</code> 与另一节点的内层端口冲突'})
                if n.get('stlsDetourPort'):
                    dp = int(n['stlsDetourPort'])
                    if dp < 1 or dp > 65535:
                        out.append({'level': 'error', 'field': 'stlsDetourPort', 'msg': '内层端口非法'})
                    elif dp == port:
                        out.append({'level': 'error', 'field': 'stlsDetourPort', 'msg': '内层端口不能和外层相同'})
        except Exception:
            out.append({'level': 'error', 'field': 'listenPort', 'msg': '端口必须是数字'})

        # UDP 区间劫持提醒
        if t in ('hysteria2', 'tuic') and 50000 <= int(n.get('listenPort') or 0) <= 51000:
            out.append({'level': 'warn', 'field': 'listenPort',
                        'msg': 'UDP 端口落在 50000-51000，若机器上有 nat REDIRECT 规则会把包劫走，'
                               '部署后请执行 <code>iptables -t nat -L PREROUTING -n</code> 核对'})

    # 证书
    need_tls = meta['tls']
    mode = (n.get('cert') or {}).get('mode', 'none')
    if need_tls == 'required' and mode == 'none':
        out.append({'level': 'error', 'field': 'cert.mode',
                    'msg': f'{meta["label"]} 必须启用 TLS，当前是「不启用」'})
    if mode != 'none':
        c = n['cert']
        if not c.get('domain'):
            out.append({'level': 'error', 'field': 'cert.domain',
                        'msg': '启用 TLS 必须填域名。自签也要填——它决定证书的 CN/SAN，'
                               '和 Xboard 下发的 SNI 对不上客户端会握手失败'})
        if mode == 'file':
            # file 模式：用户自己指定证书文件，路径必须填且文件应该存在
            if not c.get('certFile') or not c.get('keyFile'):
                out.append({'level': 'error', 'field': 'cert.certFile',
                            'msg': '证书方式为「使用已有证书文件」时，必须填证书和私钥路径'})
            else:
                if not Path(c['certFile']).exists():
                    out.append({'level': 'warn', 'field': 'cert.certFile',
                                'msg': f'证书文件 <code>{c["certFile"]}</code> 当前不存在，部署前请先申请'})
                if not Path(c['keyFile']).exists():
                    out.append({'level': 'warn', 'field': 'cert.keyFile',
                                'msg': f'私钥文件 <code>{c["keyFile"]}</code> 当前不存在'})
        elif mode == 'self':
            # self 模式：路径由部署时 ensure_self_cert 自动填充，新建节点时
            # certFile 是空的，不应该报 error 卡住保存。只在路径已有值时检查状态。
            cf = c.get('certFile') or ''
            if cf:
                info = cert_info(cf, c.get('domain'))
                if info['state'] == 'missing':
                    out.append({'level': 'info', 'field': 'cert.certFile',
                                'msg': '自签证书还没生成，部署时面板会自动补一张'
                                       '（也可以在节点页点「生成自签证书」立刻生成）'})
                elif info['state'] == 'expired':
                    out.append({'level': 'warn', 'field': 'cert.certFile',
                                'msg': f'自签证书已过期（{info["notAfter"]}），部署时不会自动覆盖，'
                                       f'请点「生成自签证书」重新生成'})
                elif info['state'] == 'mismatch':
                    out.append({'level': 'warn', 'field': 'cert.certFile',
                                'msg': f'证书里的域名（{info["cn"]}）和当前填的 '
                                       f'<code>{c.get("domain")}</code> 不一致，客户端会报 SNI 不匹配'})
            else:
                out.append({'level': 'info', 'field': 'cert.certFile',
                            'msg': '自签证书还没生成，部署时面板会自动补一张'
                                   '（也可以在节点页点「生成自签证书」立刻生成）'})
        if mode == 'dns':
            prov = c.get('provider')
            if not prov:
                out.append({'level': 'error', 'field': 'cert.provider', 'msg': 'DNS 模式必须选 provider'})
            else:
                state, detail = check_env(prov, c.get('dnsEnv'))
                pf = provider_fields(prov)
                flabel = pf.get('label') or prov
                fdefs = pf.get('fields') or {}
                def _fname(k):
                    return (fdefs.get(k) or {}).get('label') or k
                if state == 'missing':
                    need = ' 或 '.join(
                        '+'.join(f'<b>{_fname(k)}</b>' for k in grp)
                        for grp in PROVIDER_ENV.get(prov, []))
                    out.append({'level': 'error', 'field': 'cert.dnsEnv',
                                'msg': f'{flabel} 还缺凭据：{need}。'
                                       f'就在下面「{flabel}」那几栏里填，填一次会记住，'
                                       f'以后新建节点自动复用'})
                elif state == 'partial':
                    miss = detail.replace('缺少 ', '').replace('<code>', '').replace('</code>', '')
                    out.append({'level': 'warn', 'field': 'cert.dnsEnv',
                                'msg': f'{flabel} 的凭据只填了一半（{miss}）。'
                                       f'如果用 API Token 这一种，忽略这条；'
                                       f'如果用 Global Key，邮箱和 Key 要一起填'})
        if mode == 'http':
            out.append({'level': 'info', 'field': 'cert.mode',
                        'msg': 'HTTP-01 需要 80 端口可被外网访问，且该域名不能被 CF 代理（小黄云必须关闭）'})

    # 协议细节
    if t == 'shadowsocks' and n.get('owner') == 'singbox':
        m = n.get('method') or ''
        if not m.startswith('2022-'):
            out.append({'level': 'warn', 'field': 'method',
                        'msg': '非 2022 系列的加密方式安全性较低，建议用 <code>2022-blake3-aes-256-gcm</code>'})
        if n.get('stlsDetourPort'):
            pw = n.get('password') or ''
            try:
                if pw:
                    raw = base64.b64decode(pw)
                    if len(raw) != 32:
                        out.append({'level': 'error', 'field': 'password',
                                    'msg': f'SS2022 主密码解码后应为 32 字节，当前 {len(raw)} 字节'})
            except Exception:
                out.append({'level': 'error', 'field': 'password',
                            'msg': 'SS2022 密码必须是 Base64 编码的 32 字节'})
    if t in ('hysteria2', 'tuic'):
        up = n.get('upMbps') or 0
        down = n.get('downMbps') or 0
        if down > 1000:
            out.append({'level': 'warn', 'field': 'downMbps',
                        'msg': '下行带宽超过 1000 Mbps，Brutal 拥塞控制下虚高会适得其反'})
        if up and down and up > down:
            out.append({'level': 'warn', 'field': 'upMbps', 'msg': '上行大于下行，确认没填反'})
    if t in ('vless', 'vmess', 'trojan') and n.get('owner') == 'singbox':
        if n.get('transport') == 'ws' and not n.get('path'):
            out.append({'level': 'error', 'field': 'path', 'msg': 'WS 传输必须填 path'})

    # 出口
    oc = n.get('outbound') or {}
    if oc.get('type') == 'socks5':
        s5 = oc.get('socks5') or {}
        if not (s5.get('server') or '').strip():
            out.append({'level': 'error', 'field': 'outbound.socks5.server',
                        'msg': '出口选了 SOCKS5，必须填服务器地址'})
        try:
            p = int(s5.get('port') or 0)
            if p < 1 or p > 65535:
                out.append({'level': 'error', 'field': 'outbound.socks5.port',
                            'msg': 'SOCKS5 端口非法（1-65535）'})
        except Exception:
            out.append({'level': 'error', 'field': 'outbound.socks5.port',
                        'msg': 'SOCKS5 端口必须是数字'})

    return out

def validate_global(nodes, g):
    out = []
    if not g.get('apiHost', '').strip():
        out.append({'level': 'error', 'field': 'apiHost', 'msg': '面板地址（ApiHost）不能为空'})
    elif not re.match(r'^https?://', g['apiHost']):
        out.append({'level': 'error', 'field': 'apiHost', 'msg': 'ApiHost 必须以 http:// 或 https:// 开头'})
    if not g.get('apiKey'):
        out.append({'level': 'error', 'field': 'apiKey', 'msg': 'ApiKey 不能为空'})
    if not any(n.get('owner') == 'v2bx' for n in nodes):
        out.append({'level': 'info', 'field': 'nodes', 'msg': '当前没有任何 V2bX 节点，V2bX 服务将无可运行内容'})
    # 跨服务端口冲突
    used = {}
    for n in nodes:
        if n.get('owner') != 'singbox':
            continue
        p = int(n.get('listenPort') or 0)
        if p and p in used:
            out.append({'level': 'error', 'field': 'listenPort',
                        'msg': f'端口 <code>{p}</code> 同时被「{used[p]}」和「{n.get("remark") or n.get("nodeType")}」使用'})
        elif p:
            used[p] = n.get('remark') or n.get('nodeType')
    return out

# ============================================================
# 配置读写 + 事务应用
# ============================================================

class Store:
    def __init__(self):
        self.lock = threading.Lock()

    # ---------- 读取 ----------
    def read_json(self, path: Path, default):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return default
        except Exception as e:
            raise RuntimeError(f'配置文件 {path} 解析失败: {e}')

    def global_cfg(self, cfg):
        g = {'apiHost': '', 'apiKey': '', 'logLevel': 'error', 'core': 'sing'}
        if cfg:
            g['logLevel'] = (cfg.get('Log') or {}).get('Level', 'error')
            cores = cfg.get('Cores') or []
            for c in cores:
                if c.get('Type') in ('xray', 'sing'):
                    g['core'] = c['Type']
                    break
            for nd in cfg.get('Nodes') or []:
                if nd.get('ApiHost'):
                    g['apiHost'] = nd['ApiHost']
                    g['apiKey'] = MASK if nd.get('ApiKey') else ''
                    break
        return g

    def load_all(self):
        v2bx_cfg = self.read_json(V2BX_CONFIG, {})
        sb_cfg = self.read_json(SINGBOX_CONFIG, {})
        nodes = parse_v2bx_nodes(v2bx_cfg) + parse_singbox_nodes(sb_cfg)
        return nodes, self.global_cfg(v2bx_cfg)

    # ---------- 渲染 ----------
    def build_v2bx(self, nodes, g, old_cfg, ipv6=None):
        core = g.get('core', 'sing')
        if core not in ('xray', 'sing'):
            core = 'sing'
        cfg = dict(old_cfg) if old_cfg else {}
        cfg['Log'] = {'Level': g.get('logLevel', 'error'), 'Output': ''}
        cfg['Cores'] = [{
            'Type': core,
            'Log': {'Level': g.get('logLevel', 'error'), 'Timestamp': True},
            'NTP': {'Enable': False, 'Server': 'time.apple.com', 'ServerPort': 0},
            'OriginalPath': f'/etc/V2bX/{core}_origin.json',
        }]
        gg = dict(g)
        if gg.get('apiKey') == MASK:
            gg['apiKey'] = self._old_api_key(old_cfg)
        v6 = ipv6_enabled() if ipv6 is None else ipv6
        cfg['Nodes'] = [render_v2bx_node(n, gg, v6) for n in nodes if n.get('owner') == 'v2bx']
        return cfg

    def _old_api_key(self, old_cfg):
        for nd in (old_cfg or {}).get('Nodes') or []:
            if nd.get('ApiKey'):
                return nd['ApiKey']
        return ''

    def build_singbox(self, nodes, old_cfg, ipv6=None):
        v6 = ipv6_enabled() if ipv6 is None else ipv6
        cfg = dict(old_cfg) if old_cfg else {}
        cfg.setdefault('log', {'level': 'warn', 'timestamp': True})
        inbounds = []
        for n in nodes:
            if n.get('owner') != 'singbox':
                continue
            # 保留原有 users（由同步脚本维护，面板不碰）
            for ib in render_singbox_inbounds(n, v6):
                old = self._find_old_inbound(old_cfg, ib)
                if old and old.get('users') and not ib.get('users'):
                    ib['users'] = old['users']
                inbounds.append(ib)
        cfg['inbounds'] = inbounds
        # dict(old_cfg) 是浅拷贝，outbounds 列表还是同一个对象。下面要改
        # domain_strategy，先复制一份，否则会把调用方手里的 old_cfg 一起改掉
        # （回滚/对比时会看到莫名其妙的差异）。
        if not isinstance(cfg.get('outbounds'), list):
            cfg['outbounds'] = [{'type': 'direct', 'tag': 'direct'}]
        cfg['outbounds'] = [dict(o) if isinstance(o, dict) else o for o in cfg['outbounds']]
        tags = {o.get('tag') for o in cfg['outbounds'] if isinstance(o, dict)}
        if 'direct' not in tags:
            cfg['outbounds'].append({'type': 'direct', 'tag': 'direct'})
            tags.add('direct')
        # 出站域名策略：没有 IPv6 出口就钉死 ipv4_only，否则每条连接都要先
        # 吃一个 unreachable 再回落
        ds = 'prefer_ipv4' if v6 else 'ipv4_only'
        for o in cfg['outbounds']:
            if isinstance(o, dict) and o.get('type') == 'direct':
                o['domain_strategy'] = ds
        # handshake 的 detour 必须指向真实存在的 outbound，否则 sing-box 启动即报错
        for ib in inbounds:
            hs = ib.get('handshake')
            if hs and hs.get('detour') not in tags:
                hs['detour'] = 'direct'
        return cfg

    def _find_old_inbound(self, old_cfg, new_ib):
        for ib in (old_cfg or {}).get('inbounds') or []:
            if ib.get('type') == new_ib.get('type') and ib.get('listen_port') == new_ib.get('listen_port'):
                return ib
        return None

    # ---------- 应用 ----------
    def apply(self, nodes, g, target='all'):
        """
        事务性应用。返回 {ok, errors, rolled_back, logs, steps}
        """
        with self.lock:
            steps = []
            old_v2bx = self.read_json(V2BX_CONFIG, {})
            old_sb = self.read_json(SINGBOX_CONFIG, {})

            # 没装独立 sing-box 的机器（新装的基本都是），不能碰那套配置和服务
            if target in ('all', 'singbox') and not singbox_installed():
                if target == 'singbox':
                    return {'ok': False, 'stage': 'precheck', 'steps': [],
                            'errors': [{'level': 'error', 'field': '', 'msg':
                                        f'这台机器没有独立的 sing-box 实例（{SINGBOX_CONFIG} 不存在），'
                                        f'无法单独保存 sing-box 配置。V2bX 自己的节点请在「节点」页保存。'}]}
                target = 'v2bx'
                steps.append({'name': '跳过 sing-box', 'ok': True,
                              'detail': '未安装独立实例，本次只写 V2bX 配置'})

            for n in nodes:
                resolve_creds(n)
                # 补全 self 模式的证书路径：V2bX 的 requestCert() 对 self
                # 也要求 CertFile / KeyFile 不为空（node/cert.go:55-56），
                # 不补的话部署后 V2bX 会报 "cert file path or key file path
                # not exist" 然后无限重启。validate_node 里也会调一次，
                # 但 validate 不回写，build_v2bx 拿到的还是空路径。
                normalize_cert(n)

            # ---- 1. 校验 ----
            # 只校验本次真正要写的那一侧。否则 sing-box 那边一个历史遗留的
            # 小毛病会挡住 V2bX 的保存（反过来也一样），用户根本不知道
            # 为什么「改的是这边，报错的却是那边」。
            scope = [n for n in nodes
                     if target == 'all'
                     or (target == 'v2bx' and n.get('owner') == 'v2bx')
                     or (target == 'singbox' and n.get('owner') == 'singbox')]
            errs = validate_global(nodes, g)
            for n in scope:
                errs += validate_node(n, scope, g)
            hard = [e for e in errs if e['level'] == 'error']
            if hard:
                steps.append({'name': '配置校验', 'ok': False, 'detail': f'{len(hard)} 个错误'})
                return {'ok': False, 'stage': 'validate', 'errors': errs, 'steps': steps}

            steps.append({'name': '配置校验', 'ok': True, 'detail': f'{len(scope)} 个节点通过'})

            # ---- 2. 生成新配置 ----
            v6 = ipv6_enabled()
            try:
                new_v2bx = self.build_v2bx(nodes, g, old_v2bx, v6) if target in ('all', 'v2bx') else old_v2bx
                new_sb = self.build_singbox(nodes, old_sb, v6) if target in ('all', 'singbox') else old_sb
            except Exception as e:
                return {'ok': False, 'stage': 'render', 'errors': [
                    {'level': 'error', 'field': '', 'msg': f'生成配置失败: {e}'}], 'steps': steps}

            # ---- 3. 备份 ----
            baks = {}
            if target in ('all', 'v2bx') and V2BX_CONFIG.exists():
                baks['v2bx'] = backup_file(V2BX_CONFIG, 'v2bx')
            if target in ('all', 'singbox') and SINGBOX_CONFIG.exists():
                baks['singbox'] = backup_file(SINGBOX_CONFIG, 'singbox')
            steps.append({'name': '备份原配置', 'ok': True,
                          'detail': '、'.join(b.name for b in baks.values()) or '无原文件'})

            # ---- 3.5 准备自签证书 ----
            # 证书缺失时直接部署，V2bX 起来会因为 TLS 没证书而挂掉，然后回滚，
            # 表现就是「保存不了」。这里在写配置之前先补齐，一次成功。
            made = []
            for n in scope:
                c = n.get('cert') or {}
                if (c.get('mode') or 'none') != 'self':
                    continue
                domain = (c.get('domain') or '').strip()
                if not domain:
                    continue
                info = cert_info(c.get('certFile'), domain)
                if info['state'] in ('ok', 'expiring'):
                    continue
                r = self_sign(domain, c.get('certFile'), c.get('keyFile'))
                if not r.get('ok'):
                    return {'ok': False, 'stage': 'cert', 'steps': steps + [
                        {'name': '生成自签证书', 'ok': False, 'detail': domain}],
                        'errors': [{'level': 'error', 'field': 'cert.certFile',
                                    'msg': f'<code>{domain}</code> 的自签证书生成失败：'
                                           f'<br><code>{(r.get("log") or "")[-600:]}</code>'}]}
                made.append(f'{domain}（{info["state"]}）')
            if made:
                steps.append({'name': '生成自签证书', 'ok': True, 'detail': '、'.join(made)})

            # ---- 3.6 sing 内核模板 ----
            # V2bX 的 sing 内核读 OriginalPath，文件不存在直接启动失败
            # （core/sing/sing.go:55 read original config error）。安装脚本一直
            # 想 cp example/sing_origin.json，但仓库里根本没这个文件，新机必踩空。
            # 这里兜底创建，同时把出站/解析的域名策略按 IPv6 能力钉死。
            if target in ('all', 'v2bx') and g.get('core', 'sing') == 'sing':
                if SING_ORIGIN.exists():
                    backup_file(SING_ORIGIN, 'singorigin')
                changed = ensure_sing_origin(v6, nodes)
                steps.append({'name': 'sing 内核模板', 'ok': True,
                              'detail': ('出站域名策略 → ' + ('prefer_ipv4（双栈）' if v6 else 'ipv4_only（本机无 IPv6）'))
                                        if changed else '无需改动'})

            # ---- 4. 写临时文件并做语法/语义校验 ----
            tmp_v2bx = V2BX_CONFIG.with_suffix('.json.tmp')
            tmp_sb = SINGBOX_CONFIG.with_suffix('.json.tmp')
            if target in ('all', 'v2bx'):
                tmp_v2bx.write_text(json.dumps(new_v2bx, ensure_ascii=False, indent=2))
                try:
                    json.loads(tmp_v2bx.read_text())
                except Exception as e:
                    tmp_v2bx.unlink(missing_ok=True)
                    return {'ok': False, 'stage': 'json', 'errors': [
                        {'level': 'error', 'field': '', 'msg': f'V2bX 配置 JSON 非法: {e}'}], 'steps': steps}

            if target in ('all', 'singbox') and Path(SINGBOX_BIN).exists():
                tmp_sb.write_text(json.dumps(new_sb, ensure_ascii=False, indent=2))
                code, out = run([SINGBOX_BIN, 'check', '-c', str(tmp_sb)], timeout=30)
                if code != 0:
                    tmp_sb.unlink(missing_ok=True)
                    if 'tmp_v2bx' in dir() and tmp_v2bx.exists():
                        tmp_v2bx.unlink(missing_ok=True)
                    err_lines = [l for l in out.splitlines() if 'FATAL' in l or 'ERROR' in l or 'error' in l][:6]
                    return {'ok': False, 'stage': 'singbox-check', 'errors': [
                        {'level': 'error', 'field': '', 'msg': f'sing-box 校验失败：<br>' +
                         '<br>'.join(f'<code>{l}</code>' for l in err_lines) or out[:400]}],
                        'steps': steps + [{'name': 'sing-box check', 'ok': False, 'detail': '失败'}]}
                steps.append({'name': 'sing-box check', 'ok': True, 'detail': '通过'})

            # ---- 5. 原子替换 ----
            if target in ('all', 'v2bx'):
                os.replace(tmp_v2bx, V2BX_CONFIG)
            if target in ('all', 'singbox'):
                SINGBOX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                tmp_sb.write_text(json.dumps(new_sb, ensure_ascii=False, indent=2))
                os.replace(tmp_sb, SINGBOX_CONFIG)
            steps.append({'name': '写入配置', 'ok': True, 'detail': '已原子替换'})

            # ---- 6. 重启 + 观察 ----
            restarted = []
            if target in ('all', 'v2bx'):
                svc_restart(V2BX_SERVICE)
                restarted.append(V2BX_SERVICE)
            if target in ('all', 'singbox'):
                svc_restart(SINGBOX_SERVICE)
                restarted.append(SINGBOX_SERVICE)

            failed = []
            if restarted:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(restarted)) as ex:
                    futs = {s: ex.submit(wait_stable, s) for s in restarted}
                    for s in restarted:
                        try:
                            ok_s = futs[s].result(timeout=30)
                        except Exception:
                            ok_s = False
                        if ok_s:
                            steps.append({'name': f'{s} 启动', 'ok': True, 'detail': '稳定运行'})
                        else:
                            steps.append({'name': f'{s} 启动', 'ok': False,
                                          'detail': f'状态 {svc_active(s)}'})
                            failed.append(s)

            if failed:
                # ---- 7. 自动回滚 ----
                for key, path, svc in [('v2bx', V2BX_CONFIG, V2BX_SERVICE),
                                       ('singbox', SINGBOX_CONFIG, SINGBOX_SERVICE)]:
                    if key in baks:
                        shutil.copy2(baks[key], path)
                for s in restarted:
                    svc_restart(s)
                time.sleep(2)
                logs = '\n\n'.join(f'===== {s} =====\n' + get_logs(s, 80) for s in failed)
                steps.append({'name': '自动回滚', 'ok': True, 'detail': '已恢复到上一个可用配置'})
                return {
                    'ok': False, 'stage': 'runtime', 'rolled_back': True,
                    'errors': [{'level': 'error', 'field': '', 'msg':
                                f'{", ".join(failed)} 启动失败，已自动回滚。错误摘要见下方日志。'}],
                    'errors_lines': {s: extract_errors(get_logs(s, 120)) for s in failed},
                    'logs': logs, 'steps': steps,
                }

            # ---- 8. 成功 ----
            merge_creds(nodes)
            # 端口改了但跳跃规则还指着旧端口时，客户端带 mport 会全部超时。
            # 这一步静默修正，没改动就不出现在步骤里。
            try:
                hop = sync_port_hop()
                if hop:
                    steps.append({'name': '端口跳跃规则', 'ok': True,
                                  'detail': '目标端口已跟随实际监听端口：' + '、'.join(hop)})
            except Exception:
                pass
            logs = '\n\n'.join(f'===== {s} =====\n' + get_logs(s, 30) for s in restarted)
            return {'ok': True, 'steps': steps, 'logs': logs,
                    'errors': errs,
                    'ports': self.current_ports()}

    def current_ports(self):
        lp = listen_ports()
        out = []
        for (proto, port), proc in sorted(lp.items(), key=lambda x: x[0][1]):
            if 'V2bX' in proc or 'sing-box' in proc:
                out.append({'proto': proto, 'port': port, 'proc': proc})
        return out

# ============================================================
# 对接 Xboard / V2board
# ============================================================

PROBE_PATH = '/api/v1/server/UniProxy/config'


def _tls_ctx():
    """面板常部署在自签证书后面，这里不校验证书链，只做传输加密"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get_json(url, timeout=12):
    req = urllib.request.Request(
        url, headers={'Accept': 'application/json',
                      'User-Agent': 'v3bx-panel/1.0'},
        method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_tls_ctx()) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)


def normalize_host(h):
    h = (h or '').strip().rstrip('/')
    if h and not h.startswith(('http://', 'https://')):
        h = 'https://' + h
    return h


def probe_node(host, key, node_id, node_type):
    """
    拿 host + key + node 三元组去问一次 Xboard，验证是否真的能下发配置。
    返回 (ok, msg, detail)
    """
    host = normalize_host(host)
    if not host:
        return False, '面板地址没填', {}
    if not key or key == MASK:
        return False, 'API Key 没填（已保存过的密钥不会回显，请重新输入）', {}
    try:
        nid = int(node_id)
    except Exception:
        return False, '节点 ID 必须是数字', {}

    # Xboard / V2board 的节点配置接口是 GET，参数走 query：node_type / node_id / token
    qs = urlencode({'node_type': node_type, 'node_id': nid, 'token': key})
    code, body = http_get_json(f'{host}{PROBE_PATH}?{qs}')

    if code == 0:
        return False, f'连不上 {host}：{body[:140]}', {}

    try:
        d = json.loads(body)
    except Exception:
        d = None

    if code == 200 and isinstance(d, dict) and 'server_port' in d:
        # 域名藏在 tls_settings.server_name 里，不在顶层——只读顶层会一直拿到空串，
        # 于是新节点永远补不上域名，保存必然卡在「必须填域名」。
        ts = d.get('tls_settings')
        if not isinstance(ts, dict):
            ts = {}
        base = d.get('base_config')
        if isinstance(base, dict):
            bts = base.get('tls_settings')
            if isinstance(bts, dict):
                for k, v in bts.items():
                    ts.setdefault(k, v)
        name = (d.get('server_name') or ts.get('server_name') or '').strip()
        port = d.get('server_port')
        detail = {
            'serverName': name,
            'serverPort': port,
            'protocol': d.get('protocol') or node_type,
            'tls': d.get('tls'),
            'network': d.get('network'),
            'allowInsecure': ts.get('allow_insecure'),
        }
        msg = f'通：{name}:{port}' if name else f'通：端口 {port}'
        if d.get('tls') and not name:
            msg += '（面板没下发 SNI，需手填域名）'
        return True, msg, detail

    msg = ''
    if isinstance(d, dict):
        msg = d.get('message') or d.get('error') or d.get('msg') or ''
    if not msg:
        msg = (body or '').strip()[:160]
    hint = {
        401: 'API Key 不对',
        403: 'API Key 不对，或该节点未分配给这个 Key',
        404: '接口不存在，确认地址是 Xboard 面板根地址',
        422: '节点 ID 或类型不被接受',
        500: '面板端报错，检查面板日志',
    }.get(code, '')
    return False, f'HTTP {code}：{msg or hint}', {'httpCode': code}


def read_connect():
    """从 /etc/V2bX/config.json 读当前对接状态"""
    try:
        cfg = json.loads(V2BX_CONFIG.read_text())
    except FileNotFoundError:
        return {
            'apiHost': '', 'apiKey': '', 'hasKey': False, 'core': 'sing',
            'configExists': False, 'nodes': [],
            'service': svc_status(V2BX_SERVICE), 'path': str(V2BX_CONFIG),
        }
    except Exception as e:
        return {'error': f'config.json 解析失败：{e}', 'configExists': True,
                'path': str(V2BX_CONFIG)}

    core = 'sing'
    for c in cfg.get('Cores') or []:
        if c.get('Type') in ('xray', 'sing'):
            core = c['Type']
            break

    raw_nodes = cfg.get('Nodes') or []
    real_nodes = [nd for nd in raw_nodes if not is_template_node(nd)]

    host, has_key = '', False
    for nd in real_nodes:
        if nd.get('ApiHost'):
            host = nd['ApiHost']
            has_key = bool(nd.get('ApiKey'))
            break

    return {
        'apiHost': host,
        'apiKey': MASK if has_key else '',
        'hasKey': has_key,
        'core': core,
        'configExists': True,
        'path': str(V2BX_CONFIG),
        'service': svc_status(V2BX_SERVICE),
        'templateIgnored': len(raw_nodes) - len(real_nodes),
        'nodes': [{'nodeId': nd.get('NodeID', 0),
                   'nodeType': nd.get('NodeType', 'vless'),
                   'core': nd.get('Core', core)} for nd in real_nodes],
    }


def saved_api_key():
    """从原始 config.json 里取真实 ApiKey（store 层返回的是掩码）"""
    try:
        cfg = json.loads(V2BX_CONFIG.read_text())
    except Exception:
        return ''
    for nd in cfg.get('Nodes') or []:
        if nd.get('ApiKey'):
            return nd['ApiKey']
    return ''


def pick_core(node_type, prefer):
    """按协议挑一个合法内核。用户选的优先，不合法就退到协议支持的第一个。

    之前是直接 `n['core'] = 对接页选的内核`，于是「对接页选 xray + 节点是
    hysteria2」会写出一个 V2bX 永远起不来的配置，而对接页没有改内核的入口，
    用户只能卡在这里。
    """
    cores = (PROTOCOLS.get(node_type) or {}).get('cores') or ['sing']
    return prefer if prefer in cores else cores[0]


def merge_connect_nodes(old_nodes, items, core, domains=None):
    """
    把对接页提交的 [{nodeId, nodeType}] 合并进现有节点模型。
    已有的节点保留证书、监听等全部参数，只改 NodeType；新增的给默认值。

    domains: {nodeId: server_name}，从 Xboard 探测回来的 SNI，用来补空域名。
    """
    domains = domains or {}
    by_id = {}
    for n in old_nodes:
        if n.get('owner') == 'v2bx':
            by_id[int(n.get('nodeId') or 0)] = n

    out = [n for n in old_nodes if n.get('owner') != 'v2bx']
    seen = set()
    for it in items:
        try:
            nid = int(it.get('nodeId') or 0)
        except Exception:
            continue
        if nid <= 0 or nid in seen:
            continue
        seen.add(nid)
        nt = it.get('nodeType') or 'vless'
        meta = PROTOCOLS.get(nt) or {}
        if nid in by_id:
            n = json.loads(json.dumps(by_id[nid]))
            changed = n.get('nodeType') != nt
            n['nodeType'] = nt
            n['core'] = pick_core(nt, core)
            if changed:
                c = n.setdefault('cert', {})
                if meta.get('tls') == 'none':
                    c['mode'] = 'none'
                elif (c.get('mode') or 'none') == 'none':
                    # 换成必 TLS 的协议，原来没开 TLS，先给自签顶上
                    c['mode'] = 'self'
        else:
            n = blank_node('v2bx', nt)
            n['nodeId'] = nid
            n['core'] = pick_core(nt, core)

        # 探测到的 SNI 补进域名，但绝不覆盖用户已经填过的
        dom = (domains.get(nid) or '').strip()
        c = n.setdefault('cert', {})
        if dom and not (c.get('domain') or '').strip():
            c['domain'] = dom
        normalize_cert(n)
        out.append(n)
    return out


# ============================================================
# 证书
# ============================================================
#
# 三条路径，各有各的坑，这里一次说清：
#
# 1) self  —— 面板用 openssl 自己签。V2bX 也会签（node/cert.go 的
#    generateSelfSslCertificate），但它只在「文件不存在」时才动手，签完
#    没有回执，改域名也不会重签，面板这边看不到任何状态。自己签的好处：
#    点完立刻有结果、能读有效期/SAN、域名一改路径自动跟着换。
#    （顺手也把上游那行写错的 PEM 标签 "EC PRIVATE KEY" 修成了
#    "RSA PRIVATE KEY"——Go 能容错，openssl/nginx 不能。）
#
# 2) dns/http —— 优先 acme.sh（如果装了）。注意 acme.sh 的插件名和 lego 的
#    provider 名不是一套：lego 叫 cloudflare，acme.sh 叫 dns_cf。之前直接
#    把 cloudflare 传给 --dns，acme.sh 找不到 dns_cloudflare 插件，必然失败。
#    没装 acme.sh 时不报死错，而是告诉用户「保存并重启后由 V2bX 内置 lego
#    自动申请」——V2bX 的 requestCert 在证书文件不存在时就会调 lego。
#
# 3) file —— 用户自己的证书，面板只校验存在性，不碰。

DOMAIN_RE = re.compile(
    r'^(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$')

# lego provider -> acme.sh dns 插件
ACME_DNS_PLUGIN = {
    'cloudflare': 'dns_cf',
    'alidns':     'dns_ali',
    'dns_ali':    'dns_ali',
    'dnspod':     'dns_dp',
    'dns_dp':     'dns_dp',
    'gcore':      'dns_gcore',
    'route53':    'dns_aws',
    'namecheap':  'dns_namecheap',
}


def safe_name(domain):
    return re.sub(r'[^A-Za-z0-9.\-_]', '_', (domain or '').strip()) or 'default'


def cert_paths(domain):
    """面板管理的证书落地路径（按域名区分，避免多节点互相覆盖）"""
    d = safe_name(domain)
    return CERT_DIR / f'{d}.crt', CERT_DIR / f'{d}.key'


def normalize_cert(n):
    """补齐 self 模式的证书路径。就地修改，validate / render 都会先调它。"""
    c = n.get('cert')
    if not isinstance(c, dict):
        c = n['cert'] = {}
    if (c.get('mode') or 'none') != 'self':
        return
    domain = (c.get('domain') or '').strip()
    if not domain:
        return
    crt, key = cert_paths(domain)
    cur = c.get('certFile') or ''
    # 只接管「面板目录下」或「空」的路径；用户自己填的绝对路径不动
    if not cur or Path(cur).parent == CERT_DIR:
        c['certFile'] = str(crt)
        c['keyFile'] = str(key)


def cert_info(path, domain=None):
    """读证书状态：missing / ok / expiring / expired / mismatch"""
    out = {'state': 'missing', 'notAfter': '', 'cn': '', 'days': None}
    if not path or not Path(path).exists():
        return out
    code, txt = run(['openssl', 'x509', '-in', str(path), '-noout',
                     '-enddate', '-subject'], timeout=10)
    if code != 0:
        out['state'] = 'invalid'
        return out
    m = re.search(r'notAfter=(.+)', txt)
    if m:
        raw = m.group(1).strip()
        out['notAfter'] = raw
        try:
            # 形如 Mar  1 12:00:00 2035 GMT
            exp = datetime.strptime(raw.replace('GMT', '').strip(), '%b %d %H:%M:%S %Y')
            days = (exp - datetime.now()).days
            out['days'] = days
            out['state'] = 'expired' if days < 0 else ('expiring' if days < 14 else 'ok')
        except Exception:
            out['state'] = 'ok'
    else:
        out['state'] = 'ok'
    ms = re.search(r'subject=.*?CN\s*=\s*([^,\n/]+)', txt)
    if ms:
        out['cn'] = ms.group(1).strip()

    if domain and out['state'] in ('ok', 'expiring'):
        code, san = run(['openssl', 'x509', '-in', str(path), '-noout',
                         '-ext', 'subjectAltName'], timeout=10)
        names = re.findall(r'DNS:([^,\s]+)', san or '')
        want = domain.strip().lower()
        if names:
            hit = any(nm.lower() == want for nm in names)
            if not hit and want.startswith('*.'):
                hit = any(nm.lower() == want for nm in names)
            if not hit:
                out['state'] = 'mismatch'
        elif out['cn'] and out['cn'].lower() != want:
            out['state'] = 'mismatch'
    return out


def self_sign(domain, cert_file, key_file, days=3650):
    """用 openssl 生成自签证书（EC P-256，带 SAN）。

    走临时配置文件而不是 -addext：CentOS 7 的 openssl 1.0.2 不认 -addext，
    写配置文件在 1.0.2 / 1.1.1 / 3.x 上都能用。
    """
    if not DOMAIN_RE.match(domain or ''):
        return {'ok': False, 'log': f'域名格式不合法: {domain}'}
    if not shutil.which('openssl'):
        return {'ok': False, 'log': '系统没有 openssl，无法生成自签证书（apt install openssl）'}

    cert_file, key_file = Path(cert_file), Path(key_file)
    try:
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {'ok': False, 'log': f'创建目录失败: {e}'}

    cnf = cert_file.parent / f'.{safe_name(domain)}.cnf'
    cnf.write_text(
        '[req]\n'
        'distinguished_name = dn\n'
        'x509_extensions = v3\n'
        'prompt = no\n'
        '[dn]\n'
        f'CN = {domain}\n'
        '[v3]\n'
        f'subjectAltName = DNS:{domain}\n'
        'basicConstraints = CA:FALSE\n'
        'keyUsage = digitalSignature, keyEncipherment\n'
        'extendedKeyUsage = serverAuth\n'
    )
    cmd = ['openssl', 'req', '-x509', '-nodes',
           '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
           '-keyout', str(key_file), '-out', str(cert_file),
           '-days', str(days), '-config', str(cnf)]
    code, log = run(cmd, timeout=60)
    try:
        cnf.unlink()
    except Exception:
        pass
    if code != 0 or not cert_file.exists() or not key_file.exists():
        return {'ok': False, 'log': (log or 'openssl 执行失败')[-1200:]}
    try:
        os.chmod(key_file, 0o600)
        os.chmod(cert_file, 0o644)
    except Exception:
        pass
    return {'ok': True, 'log': (log or '') + f'\n已生成自签证书，有效期 {days} 天',
            'certFile': str(cert_file), 'keyFile': str(key_file), 'mode': 'self'}


def issue_cert(domain, mode, provider, dns_env, email, out_dir, node=None):
    """签发/生成证书。返回 {ok, log, certFile, keyFile, delegated?}"""
    domain = (domain or '').strip()
    if not DOMAIN_RE.match(domain):
        return {'ok': False, 'log': f'域名格式不合法：{domain or "（空）"}。'
                                    f'支持 example.com 和 *.example.com'}

    # 凭据：前端可能传掩码，从池子里取真值
    env_vals = {}
    for k, v in (dns_env or {}).items():
        if v and v not in (MASK, '••••••••'):
            env_vals[k] = v
    if not env_vals and provider:
        env_vals = dict(load_creds().get(provider, {}))

    if mode == 'self':
        crt, key = cert_paths(domain)
        if out_dir and str(out_dir) not in ('/etc/V2bX', ''):
            crt = Path(out_dir) / f'{safe_name(domain)}.crt'
            key = Path(out_dir) / f'{safe_name(domain)}.key'
        return self_sign(domain, crt, key)

    if mode not in ('dns', 'http'):
        return {'ok': False, 'log': f'不支持的证书方式：{mode}'}

    if not Path(ACME).exists():
        # 不报死错：V2bX 内置 lego，证书文件不存在时启动即申请
        return {'ok': False, 'delegated': True,
                'log': f'这台机器没装 acme.sh（{ACME}）。\n\n'
                       f'不用装也能签：V2bX 启动时发现证书文件不存在，会用内置 lego '
                       f'按 CertConfig 里的 Provider + DNSEnv 自动申请。\n'
                       f'直接点「保存并重启」，然后看日志里的 obtain certificate 即可。\n\n'
                       f'想在这里当场看到结果的话，装一下：\n'
                       f'  curl https://get.acme.sh | sh -s email=你的邮箱'}

    plugin = ACME_DNS_PLUGIN.get(provider or '')
    if mode == 'dns' and not plugin:
        return {'ok': False, 'log': f'acme.sh 没有对应 <code>{provider}</code> 的 DNS 插件。'
                                    f'可选：' + '、'.join(sorted(set(ACME_DNS_PLUGIN.values())))}

    env = dict(os.environ)
    for k, v in env_vals.items():
        env[ACME_ENV_MAP.get(k, k)] = v

    cmd = [ACME, '--issue', '--server', 'letsencrypt']
    if mode == 'dns':
        cmd += ['--dns', plugin]
    else:
        cmd += ['--standalone']
    cmd += ['-d', domain, '--keylength', 'ec-256']
    if email:
        cmd += ['--accountemail', email]
    # 只有证书确实不存在时才加 --force，避免反复打 Let's Encrypt 的速率限制
    crt0, _ = cert_paths(domain)
    if not crt0.exists():
        cmd.append('--force')

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240, env=env)
        log = (r.stdout or '') + (r.stderr or '')
    except subprocess.TimeoutExpired:
        return {'ok': False, 'log': 'acme.sh 执行超时（240s）。DNS 生效慢时正常，'
                                    '也可能是 API 凭据没权限。'}
    if r.returncode != 0:
        return {'ok': False, 'log': log[-4000:]}

    out_dir = Path(out_dir or CERT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    fullchain = out_dir / f'{safe_name(domain)}.crt'
    keyfile = out_dir / f'{safe_name(domain)}.key'
    # 不写 --reloadcmd：申请完就重启会造成一次无谓闪断，交给「部署」统一重启
    cmd2 = [ACME, '--install-cert', '-d', domain, '--ecc',
            '--fullchain-file', str(fullchain), '--key-file', str(keyfile)]
    try:
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=90)
        log += '\n' + (r2.stdout or '') + (r2.stderr or '')
    except subprocess.TimeoutExpired:
        return {'ok': False, 'log': log + '\n安装证书超时'}

    return {'ok': fullchain.exists() and keyfile.exists(), 'log': log[-4000:],
            'certFile': str(fullchain), 'keyFile': str(keyfile), 'mode': 'file'}

# ============================================================
# HTTP 服务
# ============================================================

auth = Auth()
store = Store()

class Handler(BaseHTTPRequestHandler):
    server_version = 'V2bXPanel/1.0'
    protocol_version = 'HTTP/1.1'

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 静默

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _err(self, msg, code=400):
        self._send(code, {'error': msg})

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > 8 * 1024 * 1024:
                return None
            raw = self.rfile.read(n) if n else b''
            return json.loads(raw) if raw else {}
        except Exception:
            return None

    def _token(self):
        h = self.headers.get('Authorization', '')
        if h.startswith('Bearer '):
            return h[7:]
        return ''

    def _authed(self):
        return auth.verify(self._token())

    def _client_ip(self):
        return self.client_address[0] if self.client_address else '?'

    # ---------- 路由 ----------
    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p == '/' or p == '/index.html':
                return self._serve_ui()
            if p.startswith('/api/'):
                return self._api_get(p)
            self._err('not found', 404)
        except Exception as e:
            self._err(f'服务端异常: {e}', 500)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            if p.startswith('/api/'):
                return self._api_post(p)
            self._err('not found', 404)
        except Exception as e:
            self._err(f'服务端异常: {e}', 500)

    def do_DELETE(self):
        p = urlparse(self.path).path
        try:
            if p.startswith('/api/'):
                return self._api_delete(p)
            self._err('not found', 404)
        except Exception as e:
            self._err(f'服务端异常: {e}', 500)

    def _serve_ui(self):
        try:
            html = UI_FILE.read_text(encoding='utf-8')
        except FileNotFoundError:
            return self._send(500, 'ui.html 缺失，请与 panel.py 放在同一目录', 'text/plain; charset=utf-8')
        self._send(200, html, 'text/html; charset=utf-8')

    # ---------- GET ----------
    def _api_get(self, p):
        if p == '/api/ping':
            return self._json({'ok': True, 'need_login': not AUTH_FILE.exists()})

        if not self._authed():
            return self._err('未登录', 401)

        if p == '/api/meta':
            sb_ver = ''
            if Path(SINGBOX_BIN).exists():
                c, o = run([SINGBOX_BIN, 'version'], timeout=8)
                if c == 0:
                    sb_ver = o.strip().splitlines()[0] if o.strip() else ''
            v2bx_ver = ''
            if Path(V2BX_BIN).exists():
                c, o = run([V2BX_BIN, 'version'], timeout=8)
                if c == 0:
                    v2bx_ver = o.strip().splitlines()[0] if o.strip() else ''
            return self._json({
                'protocols': PROTOCOLS,
                'certModes': CERT_MODES,
                'providers': DNS_PROVIDERS,
                'providerEnv': {k: flatten_env_keys(k) for k in PROVIDER_ENV},
                'providerFields': {k: provider_fields(k) for k in DNS_PROVIDERS},
                'creds': masked_creds(),
                'services': {
                    'v2bx': svc_status(V2BX_SERVICE),
                    'singbox': svc_status(SINGBOX_SERVICE),
                },
                'paths': {
                    'v2bx': str(V2BX_CONFIG),
                    'singbox': str(SINGBOX_CONFIG),
                },
                'caps': {
                    'singbox_version': sb_ver,
                    'v2bx_version': v2bx_ver,
                },
            })

        if p == '/api/ipv6':
            q = parse_qs(urlparse(self.path).query)
            force = (q.get('force') or ['0'])[0] in ('1', 'true', 'yes')
            return self._json(ipv6_state(force=force))

        if p == '/api/state':
            nodes, g = store.load_all()
            return self._json({
                'nodes': nodes,
                'global': g,
                'services': {
                    'v2bx': svc_status(V2BX_SERVICE),
                    'singbox': svc_status(SINGBOX_SERVICE),
                },
                'ports': store.current_ports(),
                'ipv6': ipv6_state(),
                'time': now_str(),
            })

        if p == '/api/logs':
            q = parse_qs(urlparse(self.path).query)
            svc = (q.get('service') or ['v2bx'])[0]
            lines = int((q.get('lines') or ['120'])[0])
            name = V2BX_SERVICE if svc == 'v2bx' else SINGBOX_SERVICE
            txt = get_logs(name, min(max(lines, 10), 500))
            return self._json({'service': name, 'log': txt,
                               'errors': extract_errors(txt)})

        if p == '/api/backups':
            return self._json({'backups': list_backups()})

        if p == '/api/cert/status':
            q = parse_qs(urlparse(self.path).query)
            path = (q.get('path') or [''])[0]
            domain = (q.get('domain') or [''])[0]
            if not path:
                return self._err('缺少 path')
            return self._json(cert_info(path, domain or None))

        if p == '/api/connect':
            return self._json(read_connect())

        if p == '/api/config/raw':
            q = parse_qs(urlparse(self.path).query)
            svc = (q.get('service') or ['v2bx'])[0]
            path = V2BX_CONFIG if svc == 'v2bx' else SINGBOX_CONFIG
            try:
                txt = path.read_text()
            except Exception as e:
                return self._err(str(e), 404)
            return self._json({'path': str(path), 'content': txt})

        self._err('unknown endpoint', 404)

    # ---------- POST ----------
    def _api_post(self, p):
        if p == '/api/login':
            b = self._body() or {}
            ok, msg = auth.check_creds(b.get('user', ''), b.get('password', ''), self._client_ip())
            if not ok:
                return self._err(msg, 401)
            tok = auth.issue(b.get('user', ''))
            return self._json({'token': tok, 'user': b.get('user', '')})

        if p == '/api/logout':
            auth.revoke(self._token())
            return self._json({'ok': True})

        if not self._authed():
            return self._err('未登录', 401)

        if p == '/api/password':
            b = self._body() or {}
            ok, msg = auth.change_password(b.get('old', ''), b.get('new', ''))
            if not ok:
                return self._err(msg)
            return self._json({'ok': True, 'msg': '密码已修改，请重新登录'})

        if p == '/api/validate':
            b = self._body() or {}
            nodes = b.get('nodes') or []
            g = b.get('global') or {}
            for n in nodes:
                resolve_creds(n)
            errs = validate_global(nodes, g)
            for n in nodes:
                for e in validate_node(n, nodes, g):
                    e['uid'] = n.get('uid', '')
                    errs.append(e)
            return self._json({'errors': errs,
                               'count': {'error': sum(1 for e in errs if e['level'] == 'error'),
                                         'warn': sum(1 for e in errs if e['level'] == 'warn')}})

        if p == '/api/apply':
            b = self._body() or {}
            nodes = b.get('nodes') or []
            g = b.get('global') or {}
            target = b.get('target') or 'all'
            res = store.apply(nodes, g, target)
            return self._json(res)

        if p == '/api/preview':
            b = self._body() or {}
            nodes = b.get('nodes') or []
            g = b.get('global') or {}
            svc = b.get('service') or 'v2bx'
            try:
                if svc == 'v2bx':
                    old = store.read_json(V2BX_CONFIG, {})
                    cfg = store.build_v2bx(nodes, g, old)
                else:
                    old = store.read_json(SINGBOX_CONFIG, {})
                    cfg = store.build_singbox(nodes, old)
            except Exception as e:
                return self._err(f'生成失败: {e}')
            return self._json({'content': json.dumps(cfg, ensure_ascii=False, indent=2)})

        if p == '/api/cert/issue':
            b = self._body() or {}
            mode = b.get('mode') or 'self'
            r = issue_cert(b.get('domain', ''), mode,
                           b.get('provider', 'cloudflare'), b.get('dnsEnv') or {},
                           b.get('email', ''), b.get('outDir') or '', b.get('node'))
            return self._json(r)

        if p == '/api/ipv6':
            b = self._body() or {}
            try:
                return self._json({'ok': True, 'ipv6': set_ipv6_mode(b.get('mode') or 'auto')})
            except ValueError as e:
                return self._err(str(e))

        if p == '/api/connect/test':
            b = self._body() or {}
            key = b.get('apiKey', '') or ''
            # 输入框留空（密钥不回显）时，用配置里已保存的那把
            if not key or key == MASK:
                key = saved_api_key()
            ok, msg, detail = probe_node(
                b.get('apiHost', ''), key,
                b.get('nodeId', 0), b.get('nodeType', 'vless'))
            return self._json({'ok': ok, 'msg': msg, 'detail': detail})

        if p == '/api/connect/save':
            b = self._body() or {}
            host = normalize_host(b.get('apiHost', ''))
            key = b.get('apiKey', '') or ''
            core = b.get('core') or 'sing'
            items = b.get('nodes') or []

            if not host:
                return self._err('面板地址不能为空')
            if not items:
                return self._err('至少要有一个节点 ID')
            if core not in ('xray', 'sing'):
                return self._err(f'不支持的内核：{core}')

            nodes, g = store.load_all()
            # 密钥留空或回显掩码时，沿用配置里已有的
            if not key or key == MASK:
                key = saved_api_key()
                if not key:
                    return self._err('API Key 不能为空')

            # 保存前顺手探一遍：一是提前发现 ID/类型填错，二是把 Xboard 下发的
            # server_name 拿来补空域名——新节点没域名就没法配 TLS，而对接页
            # 压根没有域名输入框，不补的话保存必然卡在「必须填域名」。
            domains, probes = {}, []
            def _probe(it):
                ok, msg, detail = probe_node(host, key, it.get('nodeId'), it.get('nodeType'))
                return it.get('nodeId'), ok, msg, (detail or {})

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(items)))) as ex:
                for nid, ok, msg, detail in ex.map(_probe, items):
                    probes.append({'nodeId': nid, 'ok': ok, 'msg': msg})
                    if ok and detail.get('serverName'):
                        domains[nid] = detail['serverName']

            merged = merge_connect_nodes(nodes, items, core, domains)
            g2 = dict(g)
            g2.update({'apiHost': host, 'apiKey': key, 'core': core})
            res = store.apply(merged, g2, 'v2bx')
            res['probes'] = probes
            res['domains'] = domains
            return self._json(res)

        if p == '/api/rollback':
            b = self._body() or {}
            name = b.get('name', '')
            if not re.match(r'^[\w.\-]+\.bak$', name):
                return self._err('备份名不合法')
            src = BACKUP_DIR / name
            if not src.exists():
                return self._err('备份不存在', 404)
            target = V2BX_CONFIG if '.v2bx.' in name or name.startswith('config.json') else SINGBOX_CONFIG
            if 'singbox' in name:
                target = SINGBOX_CONFIG
            cur = backup_file(target, 'prerollback')
            shutil.copy2(src, target)
            svc = V2BX_SERVICE if target == V2BX_CONFIG else SINGBOX_SERVICE
            svc_restart(svc)
            ok = wait_stable(svc)
            return self._json({'ok': ok, 'service': svc,
                               'log': get_logs(svc, 60),
                               'current_backup': cur.name})

        if p == '/api/restart':
            b = self._body() or {}
            svc = b.get('service', 'v2bx')
            name = V2BX_SERVICE if svc == 'v2bx' else SINGBOX_SERVICE
            svc_restart(name)
            ok = wait_stable(name)
            return self._json({'ok': ok, 'status': svc_status(name),
                               'errors': extract_errors(get_logs(name, 120))})

        self._err('unknown endpoint', 404)

    def _api_delete(self, p):
        if not self._authed():
            return self._err('未登录', 401)
        self._err('unknown endpoint', 404)


# ============================================================
# 启动
# ============================================================

def main():
    ensure_dirs()
    auth.bootstrap()

    # 同步 acme.sh 的 DNS 凭据到环境（供后续续期）
    print(f'[面板] 监听 {PANEL_BIND}:{PANEL_PORT}')
    print(f'[面板] V2bX  配置 {V2BX_CONFIG}  服务 {V2BX_SERVICE}')
    print(f'[面板] sing-box 配置 {SINGBOX_CONFIG}  服务 {SINGBOX_SERVICE}')

    srv = ThreadingHTTPServer((PANEL_BIND, PANEL_PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[面板] 退出')
        srv.shutdown()


if __name__ == '__main__':
    main()
