# shadow-tls v3 支持说明

本项目基于 [`wyx2685/V2bX`](https://github.com/wyx2685/V2bX)，唯一改动是**让 V2bX 原生支持 Shadowsocks 的 shadow-tls v3 插件**。代码保留完整上游历史，可随时合并上游更新。

上游 V2bX 会把面板下发的 `plugin` / `plugin_opts` 丢弃，只能跑裸 SS。本项目补上了这条链路，**安装方式、配置格式、面板用法与上游完全一致**，只是替换二进制。

---

## 效果

面板里建一个 shadowsocks 节点，插件选 `shadow-tls`，参数填 `host=...;password=...;version=3`，V2bX 就会自动生成两个嵌套入站：

```
公网 0.0.0.0:2310          shadowtls  ← TLS 回源 host 指定的真实站点
   │  detour（进程内直接转交）
   ▼
127.0.0.1:12310            shadowsocks  ← 多用户、计流量、报心跳
```

外层端口 = 面板的 `server_port`，内层端口自动挑一个 `server_port + 10000` 附近的空闲回环端口，不需要任何额外配置项。

因为外层通过 `ListenOptions.Detour` 把连接交给内层，**用户认证、流量统计、在线 IP 限制、限速全部仍然落在原节点 tag 上**，与普通 SS 节点行为一致。

对外表现：端口上跑的是标准 TLS 握手，SNI 与证书来自 `host` 指定的真实站点，被动探测看到的就是一个普通 HTTPS 服务。

---

## 改了什么

| 文件 | 改动 |
|---|---|
| `api/panel/node.go` | `ShadowsocksNode` 增加 `Plugin`、`PluginOpts` 字段 |
| `core/sing/node.go` | `getInboundOptions` 增加 `innerPort` 参数；SS 入站按需改听 `127.0.0.1`；`AddNode` 挂外层、`DelNode` 先摘外层 |
| `core/sing/sing.go` | `Sing` 增加 `stlsOuter` 映射，记录节点 tag → 外层 tag，支持热更新清理 |
| `core/sing/shadowtls.go` | 新增。解析 `plugin_opts`、挑内层端口、创建/销毁 shadow-tls 入站 |

依赖的内核 `github.com/wyx2685/sing-box_mod`（sing-box 1.13 系）已经自带 shadow-tls 入站实现，所以不需要额外进程、不需要外部二进制、不需要改配置文件格式。

---

## 安装

### 一键安装

和官方 V2bX 一模一样的命令，只是仓库换成这个：

```bash
wget -N https://raw.githubusercontent.com/Minerchu/v3bx/main/install.sh && bash install.sh
```

脚本流程、管理命令、菜单编号全部与官方一致（`V2bX`、`V2bX start`、`V2bX update`、`V2bX log`……），额外多做了三件事：

- 安装包带 SHA256 校验，不匹配直接中止
- 旧二进制备份到 `/usr/local/V2bX-backup/`，保留最近 3 份
- 配置向导修正了 sing-box 1.13 的 DNS 写法，没有 IPv6 出口时自动阻断 IPv6 字面量直连

首次安装会问要不要生成配置文件，选 `y` 走向导填面板地址、API Key、节点 ID 即可；已经装过官方版的机器直接跑同一条命令，配置完全不动。

### 手动安装

同官方步骤，只换二进制：

```bash
systemctl stop V2bX
cp /usr/local/V2bX/V2bX /root/V2bX.bak.$(date +%s)
cd /tmp && unzip V2bX-linux-64.zip -d v2bx      # arm 机器用 V2bX-linux-arm64-v8a.zip
cp v2bx/V2bX /usr/local/V2bX/V2bX && chmod +x /usr/local/V2bX/V2bX
systemctl start V2bX
```

二进制为静态编译（`CGO_ENABLED=0`），不依赖目标机 glibc 版本。

---

## 面板配置

Xboard / V2board 里新建或编辑 shadowsocks 节点：

| 字段 | 值 |
|---|---|
| 类型 | shadowsocks |
| 端口 | 自定义，如 `2310` |
| 加密 | `2022-blake3-aes-256-gcm`（推荐）或其它 SS2022 / 传统算法 |
| 插件 | `shadow-tls` |
| 插件参数 | `host=www.php.net;password=你的密码;version=3` |

订阅会下发：

```yaml
- name: 节点名
  type: ss
  server: your.domain
  port: 2310
  cipher: 2022-blake3-aes-256-gcm
  password: '<serverKey>:<userKey>'
  plugin: shadow-tls
  plugin-opts:
    host: www.php.net
    password: 你的密码
    version: 3
```

客户端（mihomo / Clash Meta / 小火箭）都支持这套写法。

> `password` 是**节点级**的，所有用户共用；用户之间的区分靠内层 SS2022 的 `serverKey:userKey`，所以多用户计费、限速不受影响。

---

## 编译

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  GOEXPERIMENT=jsonv2 \
  go build -tags "sing xray with_utls with_quic" \
  -trimpath -ldflags "-s -w -buildid=" \
  -o V2bX-linux-amd64 ./
```

两个坑：

- **`GOEXPERIMENT=jsonv2`** 必须加。内核用了 Go 1.25 的 `encoding/json/v2` 实验特性，不加会报一堆 `package encoding/json/v2 is not in std`。
- **`with_utls`** 取代了旧的 `with_reality_server`。继续用旧标签会报 `unknown tag`。

交叉编译 ARM 只需换 `GOARCH=arm64`。

---

## 已知限制

**UDP 不通。** shadow-tls v3 建在 TLS over TCP 上，只转发 TCP。订阅里写的 `udp: true` 实际会失败并回退 TCP。这是协议本身的限制，任何实现都一样。

**只支持 v3。** `version` 填 1 或 2 会直接报错退出，不会静默降级。

**插件密码不跟用户走。** 见上文，`plugin_opts` 里的 password 是节点级的。

**内核版本绑定。** 依赖 `wyx2685/sing-box_mod` 提供的 `ShadowTLSInboundOptions`（含 `Users` 数组）。如果上游把内核换回不带 shadow-tls 的版本，这段代码会编译失败——那时需要重新适配。

---

## 与上游同步

本项目基于上游 `dev_new` 分支（`71277de`）。上游更新后：

```bash
git remote add upstream https://github.com/wyx2685/V2bX.git
git fetch upstream
git merge upstream/dev_new
```

改动集中在 4 个文件，冲突面很小。

---

## 验证记录

在真实节点（Oracle 凤凰城，ARM64，Ubuntu）上部署并测试：

| 项目 | 结果 |
|---|---|
| Google / YouTube / GitHub | `204` / `200` / `200`，正常 |
| 出口 IP | 节点自身 IP |
| 流量统计 | `v2_stat_server` / `v2_stat_user` 正常累加 |
| 面板在线状态 | 心跳正常，显示在线 |
| 多用户 | 171 个用户自动加载，独立计费 |
| 热更新 | 面板改插件密码后约 60 秒自动重建，新旧密码均实测 |
| IPv6 字面量访问 | 已通过 `ip_version: 6 → block` 规则快速失败，不再 5 秒超时 |
