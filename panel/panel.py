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
import sys
import json
import time
import hmac
import base64
import shutil
import secrets
import hashlib
import threading
import subprocess
import concurrent.futures
from pathlib import Path
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

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
PROTOCOLS = {
    'vless':       {'label': 'VLESS',       'cores': ['xray', 'sing'], 'tls': 'optional'},
    'vmess':       {'label': 'VMess',       'cores': ['xray', 'sing'], 'tls': 'optional'},
    'trojan':      {'label': 'Trojan',      'cores': ['xray', 'sing'], 'tls': 'required'},
    'shadowsocks': {'label': 'Shadowsocks', 'cores': ['sing'],        'tls': 'none'},
    'socks':       {'label': 'SOCKS5',      'cores': ['xray', 'sing'], 'tls': 'none'},
    'http':        {'label': 'HTTP',        'cores': ['xray', 'sing'], 'tls': 'none'},
    'hysteria2':   {'label': 'Hysteria2',   'cores': ['sing'],        'tls': 'required'},
    'tuic':        {'label': 'TUIC',        'cores': ['sing'],        'tls': 'required'},
    'anytls':      {'label': 'AnyTLS',      'cores': ['sing'],        'tls': 'required'},
}

CERT_MODES = {
    'none': '不启用 TLS',
    'file': '使用已有证书文件',
    'http': 'acme.sh HTTP-01（需 80 端口）',
    'dns':  'acme.sh DNS-01（推荐，无需 80 端口）',
    'self': '自签证书',
}

DNS_PROVIDERS = ['cloudflare', 'alidns', 'dnspod', 'dns_dp', 'dns_ali', 'gcore', 'route53', 'namecheap']

# 各 provider 需要的环境变量：外层数组是 OR，内层数组是 AND
# 例 cloudflare: (有 DNS_API_TOKEN) 或 (有 API_KEY + EMAIL)
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
# 能力探测（缓存）
# ============================================================

_cap_cache = {}

def v2bx_supports(proto: str) -> bool:
    """V2bX 的协议支持由内置 core 决定，无法从二进制静态判断。
    Go 编译产物里残留的字符串会误导（实测 77MB 二进制里 grep 到 69 处 hysteria2，
    但它并不支持 hy2 core），因此这里不做静态判定，真正的验证交给
    部署时的启动检查 + 自动回滚。"""
    return True

def singbox_supports(proto: str) -> bool:
    key = 'sb_' + proto
    if key in _cap_cache:
        return _cap_cache[key]
    ok = False
    if Path(SINGBOX_BIN).exists():
        code, out = run([SINGBOX_BIN, 'version'], timeout=10)
        if code == 0:
            _cap_cache['sb_version'] = out.strip().split('\n')[0]
        code, out = run(['strings', SINGBOX_BIN], timeout=20)
        if code == 0:
            ok = proto in out.lower()
    _cap_cache[key] = ok
    return ok

# ============================================================
# 服务操作
# ============================================================

def svc_active(name):
    code, out = run(['systemctl', 'is-active', name], timeout=5)
    return out.strip()

def svc_status(name):
    active = svc_active(name)
    enabled = run(['systemctl', 'is-enabled', name], timeout=5)[1].strip()
    code, out = run(['systemctl', 'show', name, '-p', 'ActiveEnterTimestamp', '-p', 'NRestarts'], timeout=5)
    info = {}
    for line in out.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            info[k] = v
    return {'name': name, 'active': active, 'enabled': enabled,
            'since': info.get('ActiveEnterTimestamp', '').strip(),
            'restarts': info.get('NRestarts', '0').strip()}

def svc_restart(name):
    return run(['systemctl', 'restart', name], timeout=40)

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
    """返回 {(proto, port): process}"""
    code, out = run(['ss', '-tulnp'], timeout=10)
    res = {}
    for line in out.splitlines()[1:]:
        m = re.search(r'(tcp|udp)\S*\s+\S+\s+\S+\s+(\S+)\s+', line)
        if not m:
            continue
        proto, addr = m.group(1), m.group(2)
        pm = re.search(r':(\d+)$', addr)
        if not pm:
            continue
        pm2 = re.search(r'users:\(\("([^"]+)"', line)
        res[(proto, int(pm.group(1)))] = pm2.group(1) if pm2 else ''
    return res

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
    return {
        'uid': secrets.token_hex(6),
        'owner': owner,
        'nodeId': 1,
        'nodeType': node_type,
        'core': meta['cores'][0],
        'listenIP': '::' if owner == 'v2bx' else '0.0.0.0',
        'listenPort': 443 if owner == 'singbox' else 0,
        'cert': {
            'mode': 'none' if meta['tls'] == 'none' else 'dns',
            'domain': '',
            'certFile': '/etc/V2bX/fullchain.cer',
            'keyFile': '/etc/V2bX/cert.key',
            'email': '',
            'provider': 'cloudflare',
            'dnsEnv': {},
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

def parse_v2bx_nodes(cfg):
    nodes = []
    for nd in cfg.get('Nodes', []):
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

def render_v2bx_node(n, g):
    o = {
        'Core': n['core'],
        'ApiHost': g.get('apiHost', ''),
        'ApiKey': g.get('apiKey', ''),
        'NodeID': int(n['nodeId']),
        'NodeType': n['nodeType'],
        'Timeout': 30,
        'ListenIP': n.get('listenIP') or '::',
        'SendIP': '0.0.0.0',
        'DeviceOnlineMinTraffic': 200,
        'MinReportTraffic': 0,
        'TCPFastOpen': True,
        'SniffEnabled': True,
    }
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
    return o

def render_singbox_inbounds(n):
    """一个节点 -> 一个或多个 inbound"""
    t = n['nodeType']
    port = int(n.get('listenPort') or 0)
    ip = n.get('listenIP') or '0.0.0.0'
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
    elif t == 'hysteria2':
        ib['users'] = []
        if n.get('obfs'):
            ib['obfs'] = {'type': n['obfs'], 'password': n.get('obfsPassword', '')}
        if n.get('masquerade'):
            ib['masquerade'] = n['masquerade']
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
    t = n.get('nodeType')
    meta = PROTOCOLS.get(t)
    if not meta:
        out.append({'level': 'error', 'field': 'nodeType', 'msg': f'未知协议 {t}'})
        return out

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

    # 已知风险组合：只提醒，不阻断（部署失败会自动回滚）
    if n.get('owner') == 'v2bx' and t in ('hysteria2', 'tuic', 'anytls'):
        out.append({'level': 'warn', 'field': 'nodeType',
                    'msg': f'<code>{t}</code> 需要 V2bX 内置 sing-box 支持。'
                           f'启动失败会自动回滚；想更稳妥可改用 sing-box 独立进程承载'})

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
                        'msg': '启用 TLS 必须填域名（用于签发证书和 SNI）'})
        if mode in ('file', 'self'):
            if not c.get('certFile') or not c.get('keyFile'):
                out.append({'level': 'error', 'field': 'cert.certFile',
                            'msg': '证书方式为文件/自签时，必须填证书和私钥路径'})
            elif mode == 'file':
                if not Path(c['certFile']).exists():
                    out.append({'level': 'warn', 'field': 'cert.certFile',
                                'msg': f'证书文件 <code>{c["certFile"]}</code> 当前不存在，部署前请先申请'})
                if not Path(c['keyFile']).exists():
                    out.append({'level': 'warn', 'field': 'cert.keyFile',
                                'msg': f'私钥文件 <code>{c["keyFile"]}</code> 当前不存在'})
        if mode == 'dns':
            prov = c.get('provider')
            if not prov:
                out.append({'level': 'error', 'field': 'cert.provider', 'msg': 'DNS 模式必须选 provider'})
            else:
                state, detail = check_env(prov, c.get('dnsEnv'))
                if state == 'missing':
                    need = ' 或 '.join(
                        '+'.join(f'<code>{k}</code>' for k in grp)
                        for grp in PROVIDER_ENV.get(prov, []))
                    out.append({'level': 'error', 'field': 'cert.dnsEnv',
                                'msg': f'provider <code>{prov}</code> 缺少 API 凭据，需要：{need}'})
                elif state == 'partial':
                    out.append({'level': 'warn', 'field': 'cert.dnsEnv',
                                'msg': f'<code>{prov}</code> 凭据不完整，{detail}；'
                                       f'如果用的是 API Token 可忽略，否则签发会失败'})
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
    def build_v2bx(self, nodes, g, old_cfg):
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
        cfg['Nodes'] = [render_v2bx_node(n, gg) for n in nodes if n.get('owner') == 'v2bx']
        return cfg

    def _old_api_key(self, old_cfg):
        for nd in (old_cfg or {}).get('Nodes') or []:
            if nd.get('ApiKey'):
                return nd['ApiKey']
        return ''

    def build_singbox(self, nodes, old_cfg):
        cfg = dict(old_cfg) if old_cfg else {}
        cfg.setdefault('log', {'level': 'warn', 'timestamp': True})
        inbounds = []
        for n in nodes:
            if n.get('owner') != 'singbox':
                continue
            # 保留原有 users（由同步脚本维护，面板不碰）
            for ib in render_singbox_inbounds(n):
                old = self._find_old_inbound(old_cfg, ib)
                if old and old.get('users') and not ib.get('users'):
                    ib['users'] = old['users']
                inbounds.append(ib)
        cfg['inbounds'] = inbounds
        if 'outbounds' not in cfg:
            cfg['outbounds'] = [{'type': 'direct', 'tag': 'direct'}]
        tags = {o.get('tag') for o in cfg['outbounds']}
        if 'direct' not in tags:
            cfg['outbounds'].append({'type': 'direct', 'tag': 'direct'})
            tags.add('direct')
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

            for n in nodes:
                resolve_creds(n)

            # ---- 1. 校验 ----
            errs = validate_global(nodes, g)
            for n in nodes:
                errs += validate_node(n, nodes, g)
            hard = [e for e in errs if e['level'] == 'error']
            if hard:
                steps.append({'name': '配置校验', 'ok': False, 'detail': f'{len(hard)} 个错误'})
                return {'ok': False, 'stage': 'validate', 'errors': errs, 'steps': steps}

            steps.append({'name': '配置校验', 'ok': True, 'detail': f'{len(nodes)} 个节点通过'})

            # ---- 2. 生成新配置 ----
            try:
                new_v2bx = self.build_v2bx(nodes, g, old_v2bx) if target in ('all', 'v2bx') else old_v2bx
                new_sb = self.build_singbox(nodes, old_sb) if target in ('all', 'singbox') else old_sb
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
# 证书
# ============================================================

def issue_cert(domain, mode, provider, dns_env, email, out_dir):
    """调用 acme.sh 申请证书，返回 {ok, log, certFile, keyFile}"""
    if not Path(ACME).exists():
        return {'ok': False, 'log': f'acme.sh 不存在: {ACME}'}
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}$', domain or ''):
        return {'ok': False, 'log': '域名格式不合法'}

    env = dict(os.environ)
    for k, v in (dns_env or {}).items():
        if v and v != MASK:
            env[k] = v

    if mode == 'dns':
        cmd = [ACME, '--issue', '--dns', provider or 'dns_cf', '-d', domain,
               '--keylength', 'ec-256', '--force']
    else:
        cmd = [ACME, '--issue', '-d', domain, '--keylength', 'ec-256', '--force']

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        log = (r.stdout or '') + (r.stderr or '')
    except subprocess.TimeoutExpired:
        return {'ok': False, 'log': 'acme.sh 执行超时（180s）'}

    if r.returncode != 0:
        return {'ok': False, 'log': log}

    # 安装到目标目录
    out_dir = Path(out_dir or '/etc/V2bX')
    out_dir.mkdir(parents=True, exist_ok=True)
    fullchain = out_dir / f'{domain}.crt'
    keyfile = out_dir / f'{domain}.key'
    cmd2 = [ACME, '--install-cert', '-d', domain, '--ecc',
            '--fullchain-file', str(fullchain), '--key-file', str(keyfile),
            '--reloadcmd', f'systemctl restart {V2BX_SERVICE} || true']
    try:
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
        log += '\n' + (r2.stdout or '') + (r2.stderr or '')
    except subprocess.TimeoutExpired:
        return {'ok': False, 'log': log + '\n安装证书超时'}

    return {'ok': fullchain.exists() and keyfile.exists(), 'log': log,
            'certFile': str(fullchain), 'keyFile': str(keyfile)}

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
            r = issue_cert(b.get('domain', ''), b.get('mode', 'dns'),
                           b.get('provider', 'cloudflare'), b.get('dnsEnv') or {},
                           b.get('email', ''), b.get('outDir', '/etc/V2bX'))
            return self._json(r)

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
