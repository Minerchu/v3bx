package sing

import (
	"fmt"
	"net"
	"net/netip"
	"strconv"
	"strings"

	"github.com/InazumaV/V2bX/api/panel"
	"github.com/InazumaV/V2bX/conf"
	"github.com/sagernet/sing-box/option"
	"github.com/sagernet/sing/common/json/badoption"
)

// stlsNodeConfig 是一个启用了 shadow-tls 外层的节点参数
type stlsNodeConfig struct {
	host       string
	password   string
	version    int
	listenIP   string
	listenPort int
}

// parseShadowTLSNode 判断节点是否需要 shadow-tls 外层，并解析 plugin_opts。
// 需要外层时返回配置和内层 SS 应使用的回环端口（innerPort > 0）。
func parseShadowTLSNode(info *panel.NodeInfo, c *conf.Options) (*stlsNodeConfig, int, error) {
	if info == nil || info.Type != "shadowsocks" || info.Shadowsocks == nil {
		return nil, 0, nil
	}
	plugin := strings.TrimSpace(info.Shadowsocks.Plugin)
	if plugin == "" || strings.EqualFold(plugin, "none") {
		return nil, 0, nil
	}
	if !strings.EqualFold(plugin, "shadow-tls") && !strings.EqualFold(plugin, "shadowtls") {
		return nil, 0, fmt.Errorf("不支持的 SS 插件: %s（本版本仅支持 shadow-tls v3）", plugin)
	}

	cfg := &stlsNodeConfig{version: 3}
	for _, part := range strings.Split(info.Shadowsocks.PluginOpts, ";") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		k, v, ok := strings.Cut(part, "=")
		if !ok {
			continue
		}
		switch strings.ToLower(strings.TrimSpace(k)) {
		case "host":
			cfg.host = strings.TrimSpace(v)
		case "password":
			cfg.password = strings.TrimSpace(v)
		case "version":
			if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
				cfg.version = n
			}
		}
	}
	if cfg.host == "" {
		return nil, 0, fmt.Errorf("shadow-tls 插件参数缺少 host")
	}
	if cfg.password == "" {
		return nil, 0, fmt.Errorf("shadow-tls 插件参数缺少 password")
	}
	if cfg.version != 3 {
		return nil, 0, fmt.Errorf("shadow-tls 版本 %d 不支持，仅支持 v3", cfg.version)
	}

	cfg.listenPort = info.Common.ServerPort
	cfg.listenIP = c.ListenIP
	if cfg.listenIP == "" {
		cfg.listenIP = "0.0.0.0"
	}
	// 旧配置常把 ListenIP 写成 127.0.0.1（给独立 shadow-tls 当内层用）。
	// 现在外层由内核托管，回环地址会导致公网不可达，自动提升。
	if ip := net.ParseIP(cfg.listenIP); ip != nil && ip.IsLoopback() {
		cfg.listenIP = "0.0.0.0"
	}

	innerPort := pickInnerPort(cfg.listenPort)
	if innerPort == 0 {
		return nil, 0, fmt.Errorf("找不到可用的回环端口给 shadow-tls 内层（基准端口 %d）", cfg.listenPort)
	}
	return cfg, innerPort, nil
}

// pickInnerPort 给内层 SS 入站挑一个 127.0.0.1 上的空闲端口。
// 用 server_port 偏移 10000，避开外层监听端口。
func pickInnerPort(base int) int {
	start := base + 10000
	if start > 60000 {
		start = base - 10000
	}
	if start < 1024 {
		start = 20000
	}
	for p := start; p < start+200 && p <= 65535; p++ {
		ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(p)))
		if err == nil {
			_ = ln.Close()
			return p
		}
	}
	return 0
}

// addShadowTLS 在内层 SS 入站之上挂一个 shadow-tls v3 入站。
// 外层认证通过后通过 ListenOptions.Detour 直接把连接交给内层，
// 因此用户管理、流量统计仍然落在原 tag 上。
func (b *Sing) addShadowTLS(tag string, cfg *stlsNodeConfig) error {
	addr, err := netip.ParseAddr(cfg.listenIP)
	if err != nil {
		return fmt.Errorf("shadow-tls 外层监听地址无效 %q: %s", cfg.listenIP, err)
	}
	outerTag := tag + "-stls"
	outerOptions := &option.ShadowTLSInboundOptions{
		ListenOptions: option.ListenOptions{
			Listen:      (*badoption.Addr)(&addr),
			ListenPort:  uint16(cfg.listenPort),
			TCPFastOpen: true,
			InboundOptions: option.InboundOptions{
				// 认证通过后把连接直接交给内层 SS 入站，
				// 用户管理与流量统计仍落在原 tag 上。
				Detour: tag,
			},
		},
		Version: cfg.version,
		// v3 只认 users 数组，单 password 字段是 v1/v2 的用法
		Users: []option.ShadowTLSUser{
			{Name: "v2bx", Password: cfg.password},
		},
		Handshake: option.ShadowTLSHandshakeOptions{
			ServerOptions: option.ServerOptions{
				Server:     cfg.host,
				ServerPort: 443,
			},
		},
	}
	err = b.box.Inbound().Create(
		b.ctx,
		b.box.Router(),
		b.logFactory.NewLogger("inbound/shadowtls["+outerTag+"]"),
		outerTag,
		"shadowtls",
		outerOptions,
	)
	if err != nil {
		return fmt.Errorf("创建 shadow-tls 外层失败: %s", err)
	}
	b.stlsMu.Lock()
	b.stlsOuter[tag] = outerTag
	b.stlsMu.Unlock()
	return nil
}

// removeShadowTLS 摘掉节点的 shadow-tls 外层（若存在）
func (b *Sing) removeShadowTLS(tag string) {
	b.stlsMu.Lock()
	outer, ok := b.stlsOuter[tag]
	if ok {
		delete(b.stlsOuter, tag)
	}
	b.stlsMu.Unlock()
	if ok {
		_ = b.box.Inbound().Remove(outer)
	}
}
