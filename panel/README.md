# V2bX 节点管理面板

给 V2bX / sing-box 节点机用的 Web 管理面板。**不用再 SSH 手写 JSON，浏览器里填表加节点，写错自动拦住，改崩了自动回滚。**

本目录是 [v3bx](../README.md) 的配套工具，与主程序解耦：只读写 `/etc/V2bX/config.json` 和 `/etc/sing-shadowtls/config.json`，不改 V2bX 二进制、不改服务单元。不用它，V2bX 照常跑；用了它，加节点不用碰命令行。

- 零依赖：只用 Python 3 标准库，不装任何 pip 包
- 单进程：两个文件 + 一个 systemd 单元
- 无损读写：实测生成的 sing-box 配置与现有配置**差异 0 行**（users、tag、detour 全部保留）

![节点列表](docs/screenshots/01-nodes.png)

| 加节点 | 实时校验 |
|---|---|
| ![表单](docs/screenshots/02-form.png) | ![日志](docs/screenshots/03-logs.png) |

---

## 为什么需要它

手改 `/etc/V2bX/config.json` 的典型事故：

| 事故 | 后果 |
|------|------|
| `Cores` 里写了 `{"Type":"hysteria2"}` | `unknown core type` → 服务无限重启 → **该机所有节点一起挂** |
| 端口和另一个进程撞了 | 后启动的那个 bind 失败 → 无限重启 |
| `CertMode: "dns"` 但没填 DNSEnv | 证书签发失败 → TLS 节点全部不可用 |
| JSON 少个逗号 | 服务起不来，日志里只有一行 `unexpected end of JSON input` |

这个面板把每一条都变成了**保存前的拦截**，并且任何一次写入都走完整事务。

---

## 安装

```bash
git clone --depth 1 https://github.com/Minerchu/v3bx.git
cd v3bx/panel
bash install.sh
```

`install.sh` 只依赖同目录的 `panel.py` 和 `ui.html`，所以必须在这个目录里执行（不要单独 `curl` 这个脚本）。

交互式会问端口、用户名、密码。也可以非交互：

```bash
PANEL_PORT=18080 PANEL_USER=admin PANEL_PASSWORD='你的密码' bash install.sh
```

安装脚本会自动探测现有的 `/etc/V2bX/config.json` 和 `/etc/sing-shadowtls/config.json`，写入 `/etc/v2bx-panel/panel.env`，并注册 `v2bx-panel.service`。

装完访问 `http://<服务器IP>:<端口>/`。

> **防火墙**：云厂商安全组要放行面板端口；如果机器上有 1Panel / ufw / firewalld，也要单独放行。
> 1Panel 用户可执行：`iptables -I 1PANEL_BASIC 1 -p tcp --dport <端口> -j ACCEPT && netfilter-persistent save`

---

## 它做了什么

### 1. 一条一条加节点，字段自动适配

选中协议后，表单会自己变：

- **内核下拉框**只列出该协议支持的（`hysteria2` 只给 `sing`，选不到 `xray`）
- **证书区块**自动显示/隐藏（`shadowsocks`/`socks`/`http` 直接跳过；`trojan`/`hy2`/`tuic` 强制必须启用 TLS）
- **证书方式**选 `dns` 才出现 provider 和 API Key 输入框，选 `file` 才出现证书路径
- **协议参数**按类型出现：hy2 给带宽/obfs/伪装站，tuic 给拥塞控制，ss 给加密方式和 shadow-tls 内层端口

### 2. 填的时候就在校验，不用等部署

表单顶部实时显示**当前这个节点**的问题，边填边变。列出的每条都会指明字段名和原因，例如：

```
✕ core      内核 hysteria2 不合法。V2bX 的 Cores[].Type 只认 xray / sing，写其他值会让服务无限重启
✕ listenPort 端口 50032 已被「hysteria2 :50032」占用
! cert.dnsEnv cloudflare 凭据不完整，缺少 CLOUDFLARE_EMAIL；如果用的是 API Token 可忽略
```

### 3. 部署是事务，失败自动回滚

点「部署到服务器」后的完整链路：

```
① 校验（有 error 直接停，配置不动）
② 备份原配置到 /etc/v2bx-panel/backups/
③ 写临时文件
④ sing-box check 校验（不通过就停）
⑤ 原子替换（os.replace，不会出现写一半的配置）
⑥ 重启服务
⑦ 等 2 秒看状态 → 再等 4 秒确认没二次崩溃
⑧ 任一步失败 → 自动恢复备份 → 重启 → 把 journalctl 里的错误行提取出来给你看
```

第 ⑦ 步是关键：V2bX 崩溃时 `systemctl is-active` 返回的是 `activating` 而不是 `failed`，只查一次会被骗过去。所以要**两次确认**。

失败时的界面会直接给你这样的东西：

```
✕ V2bX 启动          状态 activating
✓ 自动回滚            已恢复到上一个可用配置

V2bX 错误行
2026-10-29T09:15:01 ERROR level=error msg="new core failed" err="unknown core type: hysteria2"
```

### 4. 证书申请内置

证书方式选 `acme.sh DNS-01`，填域名和 provider 凭据，点「立即申请证书」，面板会调 acme.sh 签发并把证书装到指定路径，然后自动把节点切成「使用已有证书文件」。

**凭据只需填一次**——会存进 `/etc/v2bx-panel/dns-creds.json`（600 权限），之后新建节点自动继承，界面上显示为掩码。

### 5. 日志和备份

- **日志页**：直接看 `journalctl -u V2bX` / `sing-shadowtls`，自动高亮错误行，顶部汇总「发现 N 条错误，最新一条：xxx」
- **备份页**：列历史备份，可一键回滚到任意版本（回滚前会再备份当前配置）

---

## 关键设计

### V2bX 节点 vs sing-box 节点

面板把两者统一成一个「节点」列表，但生成逻辑完全不同：

| | V2bX 节点 | sing-box 节点 |
|---|---|---|
| 配置内容 | 只有 `NodeID`/`NodeType`/`Core`/`CertConfig` | 完整 inbound（端口、密码、TLS、obfs…） |
| 端口谁定 | **面板下发**，所以表单里不填 | 自己填 |
| 用户列表 | V2bX 自己拉 | 由同步脚本维护，**面板不碰** |

> 这一点很重要：sing-box 的 `users` 是同步脚本（cron）维护的 171 个用户，面板重新生成配置时会**原样保留**，不会清空。

### shadowtls 一张卡片 = 两个 inbound

选 `shadowsocks` + 填「shadow-tls 内层端口」，会自动生成串联的两个 inbound：

```json
{"type":"shadowtls","tag":"stls-in","listen_port":2310,
 "handshake":{"server":"www.php.net","detour":"hs-direct"},
 "detour":"ss-in"},
{"type":"shadowsocks","tag":"ss-in","listen":"127.0.0.1","listen_port":18389,
 "method":"2022-blake3-aes-256-gcm","users":[171个]}
```

- 外层对公网，内层只绑 `127.0.0.1`
- `detour` 自动配对，不用手写 tag
- `handshake.detour` 会**保留原值**（比如 `hs-direct` 那个带 `domain_strategy: ipv4_only` 的 outbound），不会退化成默认的 `direct`

### 为什么不做二进制协议检测

一开始我尝试用 `strings V2bX | grep hysteria2` 判断二进制是否支持某协议。**这个方法是错的**：

- 77MB 的 Go 二进制里 `grep -ac hysteria2` 返回 **69** 处匹配
- 但 V2bX v1.1.0 实际并不支持 hy2 core

残留字符串会骗人。所以面板不做静态判定，改成：**部署时真跑一遍，失败自动回滚**。这比任何静态分析都准。

真正该拦的是 `Cores[].Type` 只能是 `xray` / `sing` —— 这个已经硬校验了。

---

## 安全建议

面板默认 `0.0.0.0` 监听 + HTTP 明文，**公网裸奔有风险**。三种加固方式，按推荐排序：

**① SSH 隧道（最省事，推荐）**

```bash
# 面板改成只监听本地
sed -i 's/PANEL_BIND=.*/PANEL_BIND=127.0.0.1/' /etc/v2bx-panel/panel.env
systemctl restart v2bx-panel

# 本地电脑执行（之后浏览器开 http://127.0.0.1:18080）
ssh -N -L 18080:127.0.0.1:18080 root@<服务器IP>
```

**② Nginx 反代 + HTTPS + IP 白名单**

```nginx
location / {
    allow 1.2.3.4;      # 你的固定 IP
    deny all;
    proxy_pass http://127.0.0.1:18080;
}
```

**③ 至少限制来源 IP**

```bash
iptables -I 1PANEL_BASIC 1 -p tcp --dport 18080 -s 1.2.3.4 -j ACCEPT
iptables -I 1PANEL_BASIC 2 -p tcp --dport 18080 -j DROP
netfilter-persistent save
```

面板本身有的防护：密码 PBKDF2-SHA256（20 万轮）、登录失败 10 次锁 5 分钟、会话 12 小时过期、敏感字段脱敏回传。

---

## 常用命令

```bash
systemctl status v2bx-panel
journalctl -u v2bx-panel -f
systemctl restart v2bx-panel

# 改端口/密码（改完重启生效）
vi /etc/v2bx-panel/panel.env

# 忘记密码：删掉认证文件，重启后会重新生成
rm /etc/v2bx-panel/auth.json && systemctl restart v2bx-panel
cat /etc/v2bx-panel/INITIAL_PASSWORD.txt
```

## 文件位置

| 路径 | 内容 |
|------|------|
| `/opt/v2bx-panel/panel.py` | 主程序 |
| `/opt/v2bx-panel/ui.html` | 前端界面 |
| `/etc/v2bx-panel/panel.env` | 端口、账号配置 |
| `/etc/v2bx-panel/auth.json` | 密码哈希（600） |
| `/etc/v2bx-panel/dns-creds.json` | DNS 服务商凭据池（600） |
| `/etc/v2bx-panel/backups/` | 配置备份，保留 30 天 |

## 卸载

```bash
systemctl disable --now v2bx-panel
rm -rf /opt/v2bx-panel /etc/v2bx-panel
rm /etc/systemd/system/v2bx-panel.service
systemctl daemon-reload
```

面板不会修改 V2bX / sing-box 的服务单元，卸载后节点服务照常运行。

---

## 已知限制

- **V2bX 的端口、path、密码由 Xboard 面板下发**，本面板只管「跑哪些 NodeID」。想改这些还得在 Xboard 里改。
- **Xboard 里新增节点后，仍需在本面板加一条**（因为老版本 Xboard 没有 `/api/v2/server/machine/nodes` 自动发现接口）。但至少不用再手写 JSON 了。
- **面板不管理 sing-box 的 users**，那是同步脚本的事，面板只保证重新生成配置时不破坏它。
- 证书自动续期依赖 acme.sh 自己的 cron，面板不接管。

---

## 更新面板

```bash
cd v3bx && git pull
bash panel/install.sh          # 重跑安装即覆盖 /opt/v2bx-panel 下的程序
```

配置和备份都在 `/etc/v2bx-panel/`，重装不会动。

## License

本目录代码随主仓库一同以 [MPL-2.0](../LICENSE) 发布。
