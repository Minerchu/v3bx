package xray

import (
	"fmt"

	"encoding/json"

	conf2 "github.com/InazumaV/V2bX/conf"
	"github.com/xtls/xray-core/core"
	"github.com/xtls/xray-core/infra/conf"
)

// buildOutbound builds the outbound for a node.
// Type "socks5" -> a socks outbound pointing at the configured server.
// Otherwise -> freedom (direct, the original behavior).
func buildOutbound(config *conf2.Options, tag string) (*core.OutboundHandlerConfig, error) {
	if oc := config.OutboundConfig; oc != nil && oc.Type == "socks5" && oc.Socks5 != nil && oc.Socks5.Server != "" {
		return buildSocksOutbound(config, tag)
	}
	return buildFreedomOutbound(config, tag)
}

// buildFreedomOutbound keeps the original freedom (direct) outbound.
func buildFreedomOutbound(config *conf2.Options, tag string) (*core.OutboundHandlerConfig, error) {
	outboundDetourConfig := &conf.OutboundDetourConfig{}
	outboundDetourConfig.Protocol = "freedom"
	outboundDetourConfig.Tag = tag

	// Build Send IP address
	if config.SendIP != "" {
		outboundDetourConfig.SendThrough = &config.SendIP
	}

	// Freedom Protocol setting
	var domainStrategy = "Asis"
	if config.XrayOptions != nil && config.XrayOptions.EnableDNS {
		if config.XrayOptions.DNSType != "" {
			domainStrategy = config.XrayOptions.DNSType
		} else {
			domainStrategy = "UseIP"
		}
	}
	proxySetting := &conf.FreedomConfig{
		DomainStrategy: domainStrategy,
	}
	var setting json.RawMessage
	setting, err := json.Marshal(proxySetting)
	if err != nil {
		return nil, fmt.Errorf("marshal proxy config error: %s", err)
	}
	outboundDetourConfig.Settings = &setting
	return outboundDetourConfig.Build()
}

// buildSocksOutbound builds a socks client outbound.
// xray's socks outbound settings schema:
//   { "servers": [ { "address": "...", "port": 1080, "users": [{"user":"","pass":""}] } ] }
func buildSocksOutbound(config *conf2.Options, tag string) (*core.OutboundHandlerConfig, error) {
	s := config.OutboundConfig.Socks5
	port := s.Port
	if port <= 0 || port > 65535 {
		port = 1080
	}
	server := map[string]interface{}{
		"address": s.Server,
		"port":    port,
	}
	if s.Username != "" || s.Password != "" {
		server["users"] = []map[string]string{{
			"user": s.Username,
			"pass": s.Password,
		}}
	}
	settings := map[string]interface{}{
		"servers": []interface{}{server},
	}
	rawBytes, err := json.Marshal(settings)
	if err != nil {
		return nil, fmt.Errorf("marshal socks settings error: %s", err)
	}
	raw := json.RawMessage(rawBytes)

	outboundDetourConfig := &conf.OutboundDetourConfig{}
	outboundDetourConfig.Protocol = "socks"
	outboundDetourConfig.Tag = tag
	outboundDetourConfig.Settings = &raw
	return outboundDetourConfig.Build()
}
