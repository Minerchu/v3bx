# 节点级出口（本地直连 / SOCKS5）说明

上游 V2bX 的出口是**全机一个 freedom**：所有节点跑完路由后都落到同一个默认出站，想让某个节点走别的出口只能改代码或者外面套一层。

本项目给每个节点加了独立出口：**本地直连**（原来的行为）或 **SOCKS5 代理**（支持可选用户名 / 密码），xray 和 sing 两种内核都支持，互不影响。

---

## 效果

面板里编辑任意 v2bx 节点，最下面多了一个「出口」区块：

```
节点入站  node-33
   │
   ├── xray 内核：出站 tag 与入站同名，dispatcher 路由未命中时按 tag 取
   │      └── direct（freedom） 或 socks5（protocol: socks）
   │
   └── sing 内核：面板写进 sing_origin.json 的路由规则
          └── inbound: ["node-33"] → outbound: "ob-node-33"
```

- 选「本地直连」= 和上游完全一致，不产生任何额外出站。
- 选「SOCKS5」= 这个节点的全部流量（TCP / UDP）从你填的 socks5 出去，其它节点不受影响。
- 用户名 / 密码留空 = 无鉴权的 socks5。

面板重启节点（或节点自动热更新）后生效。

---

## 改了什么

| 文件 | 改动 |
|---|---|
| `conf/node.go` | `Options` 增加 `OutboundConfig` 字段，配套 `OutboundConfig` / `Socks5Config` 两个类型 |
| `core/xray/outbound.go` | `buildOutbound` 按类型分支：`direct` 走原来的 `buildFreedomOutbound`（SendIP、DNSType 逻辑一行没改），`socks5` 走新增的 `buildSocksOutbound` |
| `core/xray/app/dispatcher/default.go` | 路由未命中时，先取「与入站同 tag 的出站」，再退默认出站 |
| `panel/panel.py` | 节点新增 `outbound` 字段（解析 / 渲染 / 校验三处补齐）；渲染 config 时写出 `OutboundConfig`；`ensure_sing_origin` 给 sing 节点写 `ob-node-<id>` 出站 + 路由规则 |
| `panel/ui.html` | 节点表单新增「出口」区块 |

节点配置长这样（不填整个字段 = 直连，老配置完全不受影响）：

```json
{
  "Core": "xray",
  "Name": "node-33",
  "OutboundConfig": {
    "Type": "socks5",
    "Socks5": {
      "Server": "1.2.3.4",
      "Port": 1080,
      "Username": "user",
      "Password": "pass"
    }
  }
}
```

> 顺带修了一个隐患：上游每个节点都会注册一个出站，但 dispatcher 永远只用**第一个**注册的默认出站，等于出口是全机共享的。现在每个节点用自己的出站。

**为什么 sing 要用 `Name`：** sing 的出口靠路由规则匹配入站 tag，tag 默认是 `[面板地址]-类型:节点ID`，面板算不出来也不该硬编码。所以渲染 config 时统一把 `Name` 置成 `node-<节点ID>`（`node/task.go`：`Name` 非空即拿它当 tag）。这对 xray 是零影响——xray 的出站本来就和入站同 tag。

---

## 面板配置

节点编辑页 → 滑到底部「出口」：

| 字段 | 值 |
|---|---|
| 出口方式 | `本地直连（走本机出口）` / `SOCKS5 代理` |
| 服务器地址 | socks5 的 IP 或域名，选 socks5 时必填 |
| 端口 | 默认 `1080`，1–65535 |
| 用户名 / 密码 | 可选，留空则无鉴权 |

选了 SOCKS5 但没填地址、或者端口越界，保存时会被拦住。

写进 `/etc/V2bX/config.json` 的形式见上文 `OutboundConfig`。

---

## 编译

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  GOEXPERIMENT=jsonv2 \
  go build -tags "sing xray with_utls with_quic" \
  -trimpath -ldflags "-s -w -buildid=" \
  -o V2bX-linux-amd64 ./
```

`GOEXPERIMENT=jsonv2` 必加，理由同 [SHADOWTLS.md](./SHADOWTLS.md#编译)。

---

## 已知限制

**xray 的路由规则优先。** 如果机器上配了 `RouteConfigPath`（route.json）且有兜底规则（比如 `"network": "tcp,udp" → 某个 outbound`），路由会先命中，节点级出口只在「没有规则命中」时生效。默认的 `example/config.json` 不配 route.json，所以默认就是节点级出口。

**sing 侧要重启内核才重读。** `sing_origin.json` 是 sing 启动时读的 `OriginalPath`，改出口后需要重启 V2bX 服务（或让节点重建）才生效。xray 侧因为出站是运行时注册的，节点热更新即生效。

**socks5 不做连通性预检。** 填了连不上的地址，节点会正常起来，但流量会超时。面板只校验格式，不拨测。

**私有地址仍然拦截。** sing 侧 `ip_is_private → block` 规则被固定在路由最前面，节点选了 socks5 也不会把内网流量放出去。

---

## 与上游同步

改动集中在 5 个文件，冲突面很小：

```bash
git remote add upstream https://github.com/wyx2685/V2bX.git
git fetch upstream
git merge upstream/dev_new
```

如果上游改了 `dispatcher/default.go` 的 `routedDispatch`，那段 `if handler == nil && inTag != ""` 需要重新对位——它是整个功能里唯一碰内核调度的地方。

---

## 验证记录

| 项目 | 结果 |
|---|---|
| 面板侧 7 组逻辑测试 | 全绿：出站生成、规则顺序、幂等、socks5 切回直连的清理、sing/xray 混合节点、地址与端口校验 |
| xray socks 出站配置结构 | 对照上游 `infra/conf/socks.go` 核对：`servers` 恰好 1 个、`users[].user/pass` 字段名一致 |
| sing-box socks 出站 | 用 `version: "5"`、`server` / `server_port` / `username` / `password`，与 sing-box 1.13 的出站字段一致 |
| Go 编译 | 待 `go build ./...` 验证 |
| 真实节点端到端 | 待实测 |
