# V2bX

**V2board / Xboard 节点服务端**，支持 Vmess / Vless / Trojan / Shadowsocks / Hysteria1&2，单实例对接多节点。

本仓库基于 [wyx2685/V2bX](https://github.com/wyx2685/V2bX)，在上游基础上做了三件事：**修断流、加面板、补 shadow-tls v3**。安装方式、配置文件格式、管理命令全部与上游一致，替换二进制即可。

[![Release](https://img.shields.io/github/v/release/Minerchu/v3bx?label=release&color=blue)](https://github.com/Minerchu/v3bx/releases)
[![License](https://img.shields.io/badge/license-MPL--2.0-green)](./LICENSE)
[![](https://img.shields.io/badge/TgChat-UnOfficialV2Board%E4%BA%A4%E6%B5%81%E7%BE%A4-green)](https://t.me/unofficialV2board)
[![](https://img.shields.io/badge/TgChat-YuzukiProjects%E4%BA%A4%E6%B5%81%E7%BE%A4-blue)](https://t.me/YuzukiProjects)

## 相对上游改了什么

| 能力 | 上游 V2bX | 本项目 |
|---|---|---|
| **shadow-tls v3** | 需自行拼双层入站 | 面板插件选 `shadow-tls`、参数填 `host=...;password=...;version=3`，自动生成 shadowtls 外层 + shadowsocks 内层，用户/流量/心跳照旧 · [说明](./SHADOWTLS.md) |
| **改限速** | 判定为「删用户 + 加用户」，踢掉该用户全部连接 | 按 uuid 比对，限速变化走热更新，连接不断 |
| **踢人 / 用户到期** | 有概率 `fatal error`，**整进程退出**，同机全节点掉线 | 加锁 + 快照 + 幂等，只关目标链路 |
| **加节点** | SSH 手改 `config.json`，写错一处整机全挂 | Web 面板填表，写错当场拦住，改崩自动回滚 · [面板](./panel/) |

> 三个改动都不涉及配置格式变更。从上游或官方版迁过来，直接跑安装命令即可，`/etc/V2bX/` 原样保留。

**注意：本项目需要搭配[修改版 V2board](https://github.com/wyx2685/v2board)**

## 软件安装

### 一键安装

命令、流程、管理菜单和官方完全一致，只是下载源换成本项目：

```bash
wget -N https://raw.githubusercontent.com/Minerchu/v3bx/main/install.sh && bash install.sh
```

脚本会自动识别系统与架构（x86_64 / aarch64）、装依赖、下载安装包、校验 SHA256、写 systemd（Alpine 用 openrc）、设置开机自启，并安装 `V2bX` 管理命令。

装完第一次会问你要不要生成配置文件，选 `y` 走向导填面板地址、API Key、节点 ID 即可。

升级时旧二进制会备份到 `/usr/local/V2bX-backup/`（保留最近 3 份），出问题可以直接换回去：

```bash
cp /usr/local/V2bX-backup/V2bX.<时间戳> /usr/local/V2bX/V2bX && systemctl restart V2bX
```

### 管理命令

和官方完全一致（大小写不敏感）：

```bash
V2bX              # 管理菜单（0-18）
V2bX start        # 启动
V2bX stop         # 停止
V2bX restart      # 重启
V2bX status       # 状态
V2bX log          # 日志
V2bX config       # 改配置
V2bX update       # 更新到最新版
V2bX update 1.1.1 # 更新到指定版本
V2bX install      # 重装
V2bX uninstall    # 卸载
V2bX version      # 版本
V2bX generate     # 重新生成配置文件
V2bX x25519       # 生成 x25519 密钥
```

### 面板管理

管理菜单 **17** 是节点管理面板的入口，主菜单底部也会直接显示面板状态和端口，不用再翻 `/etc/v2bx-panel/panel.env`：

```
面板状态: 已运行  端口 18080  http://1.2.3.4:18080/
```

进去之后：

| 选项 | 作用 |
|---|---|
| 1 | 查看面板信息（访问地址、监听、用户名、密码、配置文件路径） |
| 2 / 3 / 4 | 启动 / 停止 / 重启 |
| 5 | 面板日志（journalctl，最近 80 行） |
| 6 | 修改监听端口（自动检查占用，重启失败自动回滚） |
| 7 | 重置登录密码（改完直接给出新密码，不用手动删 auth.json） |
| 8 | 安装 / 更新面板（保留现有端口、账号、密码） |
| 9 | 卸载面板 |

也可以直接敲命令：

```bash
V2bX panel          # 面板信息
V2bX panel start    # 启动
V2bX panel stop     # 停止
V2bX panel restart  # 重启
V2bX panel log      # 日志
V2bX panel port     # 改端口
V2bX panel passwd   # 重置密码
V2bX panel install  # 安装/更新
V2bX panel uninstall# 卸载
```

> 密码只在首次安装时以明文写在 `/etc/v2bx-panel/INITIAL_PASSWORD.txt`。如果你在面板里改过密码，那个文件就过期了 —— 菜单里会显示「已修改，无法查看」，用选项 7 重置即可。

### 指定版本安装

```bash
wget -N https://raw.githubusercontent.com/Minerchu/v3bx/main/install.sh && bash install.sh v1.1.1
```

### 从官方 V2bX 迁移过来

直接跑上面的安装命令即可：配置在 `/etc/V2bX/`，安装器只替换 `/usr/local/V2bX/` 下的二进制，配置原样保留，装完自动重启。

### 手动安装

[手动安装教程](https://v2bx.v-50.me/v2bx/v2bx-xia-zai-he-an-zhuang/install/manual)

## Web 管理面板

本仓库附带一个节点管理面板（[`panel/`](./panel/)），解决「每加一个节点都要 SSH 手改 `config.json`、写错一处整机节点全挂」的问题。

```bash
git clone --depth 1 https://github.com/Minerchu/v3bx.git
cd v3bx/panel && bash install.sh
```

![节点列表](panel/docs/screenshots/01-nodes.png)

- 表单按协议自动适配字段，内核只列可选项（`hysteria2` 选不到 `xray`），证书区块按协议自动显示
- 保存前实时校验：非法 core、端口冲突、证书凭据缺失，当场标红，不进部署流程
- 部署走事务：备份 → `sing-box check` → 原子替换 → 重启 → 两次确认 → 失败自动回滚并回显错误行
- 内置 acme.sh DNS-01 证书申请，DNS 凭据存一次、后续节点自动继承

零依赖（Python 3 标准库）、单进程、systemd 托管，与 V2bX 二进制完全解耦。详见 [panel/README.md](./panel/README.md)。

## 断流修复

老版本「用着用着就断、过一会儿又好了」，在服务端这一侧有四个明确根因，本分支已全部修掉。修复思路参考作者新项目 [v2node](https://github.com/wyx2685/v2node)。

| 问题 | 触发场景 | 症状 | 修复 |
|---|---|---|---|
| `compareUserList` 用 `uuid+SpeedLimit` 拼 key | 后台改用户限速 | 该用户全部连接被 `CloseAll` | 按 uuid 比对，限速变化走 `modified` 热更新 |
| `ratelimit.Bucket` 绑死在连接上 | 改限速要生效 | 必须断连重建 | 换 `DynamicBucket`（`atomic.Value`），原地换桶 |
| `LinkManager.CloseAll` 无锁遍历 map | 踢人 / 用户到期 | `fatal error`，**进程直接退出**，同机全节点掉线 | 加锁 + 快照 + `closed` 幂等 |
| `Limiter.AliveList` 是普通 map | 定时任务替换 vs 新连接读 | `concurrent map read and write` | 加 `RWMutex`，统一走 setter |

其中第三条最要命：它不是 panic，`recover` 拦不住，一个用户被踢会让整台机器的所有节点一起掉线，日志里只留一行 `fatal error`。

完整的代码分析、复现步骤和回归测试见 **[docs/STREAM-FIX.md](./docs/STREAM-FIX.md)**。

> 顺带说明：这里修的是**服务端进程内**的断流。如果你遇到的是「用几分钟就卡住、换个 IP 又好了」，那是客户端到入口的链路问题，跟代码无关，得靠优选 IP 或换入口解决。

## 特点

- 开源免费，MPL-2.0
- 支持 Vmess / Vless / Trojan / Shadowsocks / Hysteria1&2
- 支持 Vless 与 XTLS 新特性
- 单实例对接多节点，无需重复启动
- 在线 IP 限制、TCP 连接数限制、节点级 / 用户级限速
- 多内核（xray / sing-box / hysteria2），支持条件编译，只编需要的内核
- 修改配置自动重启实例

## 功能介绍

| 功能        | v2ray | trojan | shadowsocks | hysteria1/2 |
|-----------|-------|--------|-------------|----------|
| 自动申请tls证书 | √     | √      | √           | √        |
| 自动续签tls证书 | √     | √      | √           | √        |
| 在线人数统计    | √     | √      | √           | √        |
| 审计规则      | √     | √      | √           | √         |
| 自定义DNS    | √     | √      | √           | √        |
| 在线IP数限制   | √     | √      | √           | √        |
| 连接数限制     | √     | √      | √           | √         |
| 跨节点IP数限制  |√      |√       |√            |√          |
| 按照用户限速    | √     | √      | √           | √         |
| 动态限速(未测试) | √     | √      | √           | √         |

## TODO

- [ ] 重新实现动态限速
- [ ] 完善使用文档

## 构建
``` bash
# 通过-tags选项指定要编译的内核， 可选 xray， sing, hysteria2
GOEXPERIMENT=jsonv2 go build -v -o build_assets/V2bX -tags "sing xray hysteria2 with_quic with_grpc with_utls with_wireguard with_acme with_gvisor" -trimpath -ldflags "-X 'github.com/InazumaV/V2bX/cmd.version=$version' -s -w -buildid="
```

## 配置文件及详细使用教程

[详细使用教程](https://v2bx.v-50.me/)

## 免责声明

* 此项目用于本人自用，因此本人不能保证向后兼容性。
* 由于本人能力有限，不能保证所有功能的可用性，如果出现问题请在Issues反馈。
* 本人不对任何人使用本项目造成的任何后果承担责任。
* 本人比较多变，因此本项目可能会随想法或思路的变动随性更改项目结构或大规模重构代码，若不能接受请勿使用。

## 赞助

[赞助链接](https://v-50.me/)

## Thanks

* [Project X](https://github.com/XTLS/)
* [V2Fly](https://github.com/v2fly)
* [VNet-V2ray](https://github.com/ProxyPanel/VNet-V2ray)
* [Air-Universe](https://github.com/crossfw/Air-Universe)
* [XrayR](https://github.com/XrayR/XrayR)
* [sing-box](https://github.com/SagerNet/sing-box)

## Stars 增长记录

[![Stargazers over time](https://starchart.cc/wyx2685/V2bX.svg)](https://starchart.cc/wyx2685/V2bX)
