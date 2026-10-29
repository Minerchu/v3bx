# 断流修复详解

> 老版本 V2bX「用着用着就断、过一会儿又好了」，在**服务端这一侧**有三个明确根因，本分支已全部修掉。
>
> 修复思路参考了作者新项目 [wyx2685/v2node](https://github.com/wyx2685/v2node)。
>
> 对应版本：[v1.1.1](https://github.com/Minerchu/v3bx/releases/tag/v1.1.1) · 提交 [f081da7](https://github.com/Minerchu/v3bx/commit/f081da7)

先分清两类「断流」，别混着排查：

| 现象 | 归属 | 去哪解决 |
|---|---|---|
| 后台改个限速、或者有人到期被踢，**所有人**一起断一下 | 服务端进程 | 本文（已修复） |
| 用几分钟就卡住，换个 IP 又好了，有规律 | 客户端 ↔ 入口链路 | 优选 IP / 换域名 / 换传输，跟代码无关 |

---

## 1. 改限速 = 踢掉该用户所有连接

`node/user.go` 的 `compareUserList` 原来用 `uuid + SpeedLimit` 拼比对 key：

```go
key := user.Uuid + strconv.Itoa(user.SpeedLimit)
```

后果是：后台把某个用户的限速从 0 改成 100，这一条在比对里变成「删掉旧用户 + 新增一个用户」。删除路径会走到 `DelUsers`，里面 `lm.CloseAll()` 把这个用户的全部链路关掉 —— 用户正在下载的文件直接断，客户端重连后限速才生效。

现在按 `uuid` 比对，限速/设备数变化单独归到 `modified`：

```
deleted  → 真删（用户被删 / 到期）
added    → 真加
modified → 只换限速桶、改设备数，连接不动
```

配套改了 `Limiter.UpdateUser`，新增 `modified` 参数；`node/task.go` 里 `added/deleted/modified` 任一非空才刷新 limiter。

---

## 2. 限速桶是静态的，换速率必须重建连接

`common/rate` 原来把 `*ratelimit.Bucket` 直接绑在 conn / writer 上。桶的速率在创建时就定死，想改只能换对象，而对象已经挂在活跃连接上 —— 只能断连接重建。

现在换成 `DynamicBucket`（`atomic.Value` 持桶）：

```go
type DynamicBucket struct{ v atomic.Value }

func (d *DynamicBucket) Update(rate int64) {
    d.v.Store(ratelimit.NewBucketWithQuantum(time.Second, rate, rate))
}
```

`Update` 原地换桶，已建立的连接下一次 `Wait` 就按新速率走，零中断。

同时把 `CheckLimit` 里的 `LoadOrStore` 改成「先 Load 再建」——老写法每次连接都 `NewBucketWithQuantum` 分配一个满额桶，桶已存在时这个对象直接扔掉，高并发下纯属浪费。

---

## 3. 踢人时进程直接崩（最严重）

`core/xray/app/dispatcher/linkmanager.go` 的 `CloseAll` 是无锁遍历：

```go
func (m *LinkManager) CloseAll() {
    for w, r := range m.links {   // 遍历
        common.Close(w)           // → ManagedWriter.Close → RemoveWriter → delete(m.links, ...)
        common.Interrupt(r)       //   边遍历边写 map
    }
}
```

Go 运行时对此的处理是 `fatal error: concurrent map iteration and map write`，**不是 panic，`recover` 拦不住，整个进程直接退出**。一个用户被踢，同一台机器上所有节点的所有连接一起掉。

现在先把 map 换成空表做快照，再遍历快照，`closed` 标记保证幂等：

```go
m.mu.Lock()
if m.closed {
    m.mu.Unlock()
    return
}
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

---

## 4. 在线设备数快照的并发读写

`Limiter.AliveList` 是普通 map：定时任务整体替换它，每个新连接读它做设备数判断。两者撞上就是 `concurrent map read and map write`。

现在加了 `RWMutex`，读写统一走 `SetAliveList` / `getAlive` / `deleteAlive`。

---

## 顺手清掉的死参数

`CheckLimit(taguuid, ip, isTcp, noSSUDP)` 里的 `isTcp` 在函数体里从头到尾没被用过，调用方却要为此算 `destination.Network == net.Network_TCP`。已删除，签名变成 `CheckLimit(taguuid, ip, noSSUDP)`。

---

## 测试

```bash
GOEXPERIMENT=jsonv2 go test ./node/ -run TestCompareUserList -v
GOEXPERIMENT=jsonv2 go test -race ./core/xray/app/dispatcher/ -run TestLinkManager -v
```

`TestCompareUserListSpeedLimitChange` 就是锁住「改限速不能判成增删」这条行为的回归测试。

---

## 受影响文件

| 文件 | 改动 |
|---|---|
| `node/user.go` | `compareUserList` 按 uuid 比对，返回 `deleted / added / modified` |
| `node/task.go` | 三态刷新 limiter，`AliveList` 走 setter |
| `limiter/limiter.go` | `UpdateUser` 支持 `modified`，`AliveList` 加锁 |
| `common/rate/conn.go`、`writer.go` | 静态桶 → `DynamicBucket` |
| `core/xray/app/dispatcher/linkmanager.go` | `CloseAll` 加锁 + 快照 + 幂等 |
| `core/xray/app/dispatcher/default.go` | 适配新 `CheckLimit` 签名 |
| `core/sing/hook.go`、`core/hy2/logger.go` | 同上 |
| `node/user_test.go`、`linkmanager_test.go` | 新增回归测试 |
