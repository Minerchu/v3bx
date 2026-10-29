# V2bX

> **本项目基于 [wyx2685/V2bX](https://github.com/wyx2685/V2bX)，增加了 Shadowsocks 的 shadow-tls v3 插件支持。**
>
> 面板里插件选 `shadow-tls`、参数填 `host=...;password=...;version=3` 即可生效，V2bX 会自动生成 shadowtls 外层 + shadowsocks 内层两个嵌套入站，用户管理、流量统计、心跳全部照旧。
>
> 安装方式与上游完全一致：替换二进制、配置文件不动。详见 **[SHADOWTLS.md](./SHADOWTLS.md)**。
>
> 预编译二进制见 [Releases](https://github.com/Minerchu/v3bx/releases)。

[![](https://img.shields.io/badge/TgChat-UnOfficialV2Board%E4%BA%A4%E6%B5%81%E7%BE%A4-green)](https://t.me/unofficialV2board)
[![](https://img.shields.io/badge/TgChat-YuzukiProjects%E4%BA%A4%E6%B5%81%E7%BE%A4-blue)](https://t.me/YuzukiProjects)

A V2board node server based on multi core, modified from XrayR.  
一个基于多种内核的V2board节点服务端，修改自XrayR，支持V2ay,Trojan,Shadowsocks协议。

**注意： 本项目需要搭配[修改版V2board](https://github.com/wyx2685/v2board)**

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
V2bX              # 管理菜单（0-17）
V2bX start        # 启动
V2bX stop         # 停止
V2bX restart      # 重启
V2bX status       # 状态
V2bX log          # 日志
V2bX config       # 改配置
V2bX update       # 更新到最新版
V2bX update 1.1.0 # 更新到指定版本
V2bX install      # 重装
V2bX uninstall    # 卸载
V2bX version      # 版本
V2bX generate     # 重新生成配置文件
V2bX x25519       # 生成 x25519 密钥
```

### 指定版本安装

```bash
wget -N https://raw.githubusercontent.com/Minerchu/v3bx/main/install.sh && bash install.sh v1.1.0
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

老版本 V2bX 的「用着用着就断、过一会儿又好了」有两个明确根因，已在本分支修掉。改动参考了作者新项目 [wyx2685/v2node](https://github.com/wyx2685/v2node) 的做法。

### 1. 改限速 = 踢掉该用户所有连接

`node/user.go` 的 `compareUserList` 原来用 `uuid + SpeedLimit` 拼比对 key：

```go
key := user.Uuid + strconv.Itoa(user.SpeedLimit)
```

后果是：后台把某个用户的限速从 0 改成 100，这一条在比对里变成「删掉旧用户 + 新增一个用户」。删除路径会走到 `DelUsers`，里面 `lm.CloseAll()` 把这个用户的全部链路关掉 —— 用户正在下载的文件直接断，客户端重连后限速才生效。

现在按 `uuid` 比对，限速/设备数变化单独归到 `modified`：

```
deleted  → 真删（用户被删/到期）
added    → 真加
modified → 只换限速桶、改设备数，连接不动
```

配套改了 `Limiter.UpdateUser`，新增 `modified` 参数；`node/task.go` 里 `added/deleted/modified` 任一非空才刷新 limiter。

### 2. 限速桶是静态的，换速率必须重建连接

`common/rate` 原来把 `*ratelimit.Bucket` 直接绑在 conn / writer 上。桶的速率在创建时就定死，想改只能换对象，而对象已经挂在活跃连接上 —— 只能断连接重建。

现在换成 `DynamicBucket`（`atomic.Value` 持桶）：

```go
type DynamicBucket struct{ v atomic.Value }
func (d *DynamicBucket) Update(rate int64) { d.v.Store(ratelimit.NewBucketWithQuantum(time.Second, rate, rate)) }
```

`Update` 原地换桶，已建立的连接下一次 `Wait` 就按新速率走，零中断。同时把 `CheckLimit` 里的 `LoadOrStore` 改成「先 Load 再建」——老写法每次连接都 `NewBucketWithQuantum` 分配一个满额桶，桶已存在时这个对象直接扔掉，高并发下纯属浪费。

### 3. 踢人时进程直接崩（最严重）

`core/xray/app/dispatcher/linkmanager.go` 的 `CloseAll` 是无锁遍历：

```go
func (m *LinkManager) CloseAll() {
	for w, r := range m.links {   // 遍历
		common.Close(w)           // → ManagedWriter.Close → RemoveWriter → delete(m.links, ...)
		common.Interrupt(r)       //   边遍历边写 map
	}
}
```

Go 运行时对此的处理是 `fatal error: concurrent map iteration and map write`，**不是 panic，recover 拦不住，整个进程直接退出**。一个用户被踢，同一台机器上所有节点的所有连接一起掉。

现在先把 map 换成空表做快照，再遍历快照，`closed` 标记保证幂等：

```go
m.mu.Lock()
if m.closed { m.mu.Unlock(); return }
m.closed = true
links := m.links
m.links = make(map[*ManagedWriter]buf.Reader)
m.mu.Unlock()
for w, r := range links { ... }
```

复现验证（`linkmanager_test.go`，8 协程建链 + 1 协程踢人）：

```
旧实现：fatal error: concurrent map iteration and map write   ← 进程退出
新实现：PASS（-race 无告警）
```

### 4. 在线设备数快照的并发读写

`Limiter.AliveList` 是普通 map：定时任务整体替换它，每个新连接读它做设备数判断。两者撞上就是 `concurrent map read and map write`。现在加了 `RWMutex`，读写都走 `SetAliveList` / `getAlive` / `deleteAlive`。

### 顺手清掉的死参数

`CheckLimit(taguuid, ip, isTcp, noSSUDP)` 里的 `isTcp` 在函数体里从头到尾没被用过，调用方却要为此算 `destination.Network == net.Network_TCP`。已删除，签名变成 `CheckLimit(taguuid, ip, noSSUDP)`。

### 测试

```bash
GOEXPERIMENT=jsonv2 go test ./node/ -run TestCompareUserList -v
GOEXPERIMENT=jsonv2 go test -race ./core/xray/app/dispatcher/ -run TestLinkManager -v
```

`TestCompareUserListSpeedLimitChange` 就是锁住「改限速不能判成增删」这条行为的回归测试。

## 特点

* 永久开源且免费。
* 支持Vmess/Vless, Trojan， Shadowsocks, Hysteria1/2多种协议。
* 支持Vless和XTLS等新特性。
* 支持单实例对接多节点，无需重复启动。
* 支持限制在线IP。
* 支持限制Tcp连接数。
* 支持节点端口级别、用户级别限速。
* 配置简单明了。
* 修改配置自动重启实例。
* 支持多种内核，易扩展。
* 支持条件编译，可仅编译需要的内核。

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
