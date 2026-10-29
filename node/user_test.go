package node

import (
	"testing"

	"github.com/InazumaV/V2bX/api/panel"
)

// TestCompareUserListSpeedLimitChange 回归测试：改限速不能被判成删 + 加。
//
// 老实现用 uuid + SpeedLimit 拼 key 比对，用户限速一变就变成
// deleted + added，DelUsers 会 CloseAll 掉该用户所有链路 —— 也就是
// 后台改个限速，用户正在跑的连接全断。这条测试锁住新行为：
// 限速变化必须落到 modified。
func TestCompareUserListSpeedLimitChange(t *testing.T) {
	old := []panel.UserInfo{{Id: 1, Uuid: "uuid-a", SpeedLimit: 0, DeviceLimit: 3}}
	new := []panel.UserInfo{{Id: 1, Uuid: "uuid-a", SpeedLimit: 100, DeviceLimit: 3}}

	deleted, added, modified := compareUserList(old, new)

	if len(deleted) != 0 {
		t.Fatalf("改限速不该产生 deleted，实际 %d 条：%+v", len(deleted), deleted)
	}
	if len(added) != 0 {
		t.Fatalf("改限速不该产生 added，实际 %d 条：%+v", len(added), added)
	}
	if len(modified) != 1 || modified[0].Uuid != "uuid-a" {
		t.Fatalf("改限速应产生 1 条 modified，实际 %+v", modified)
	}
}

// TestCompareUserListDeviceLimitChange 设备数变化同样走 modified。
func TestCompareUserListDeviceLimitChange(t *testing.T) {
	old := []panel.UserInfo{{Id: 2, Uuid: "uuid-b", SpeedLimit: 50, DeviceLimit: 1}}
	new := []panel.UserInfo{{Id: 2, Uuid: "uuid-b", SpeedLimit: 50, DeviceLimit: 5}}

	deleted, added, modified := compareUserList(old, new)

	if len(deleted)+len(added) != 0 {
		t.Fatalf("改设备数不该产生增删，deleted=%+v added=%+v", deleted, added)
	}
	if len(modified) != 1 {
		t.Fatalf("改设备数应产生 1 条 modified，实际 %+v", modified)
	}
}

// TestCompareUserListAddDelete 纯增删仍然要能识别出来。
func TestCompareUserListAddDelete(t *testing.T) {
	old := []panel.UserInfo{
		{Id: 1, Uuid: "keep", SpeedLimit: 10},
		{Id: 2, Uuid: "gone", SpeedLimit: 10},
	}
	new := []panel.UserInfo{
		{Id: 1, Uuid: "keep", SpeedLimit: 10},
		{Id: 3, Uuid: "fresh", SpeedLimit: 10},
	}

	deleted, added, modified := compareUserList(old, new)

	if len(deleted) != 1 || deleted[0].Uuid != "gone" {
		t.Fatalf("应删除 gone，实际 %+v", deleted)
	}
	if len(added) != 1 || added[0].Uuid != "fresh" {
		t.Fatalf("应新增 fresh，实际 %+v", added)
	}
	if len(modified) != 0 {
		t.Fatalf("未变化的用户不该进 modified，实际 %+v", modified)
	}
}

// TestCompareUserListNoChange 完全一致时三类都为空，避免无意义地刷 limiter。
func TestCompareUserListNoChange(t *testing.T) {
	users := []panel.UserInfo{
		{Id: 1, Uuid: "a", SpeedLimit: 10, DeviceLimit: 2},
		{Id: 2, Uuid: "b", SpeedLimit: 0, DeviceLimit: 0},
	}

	deleted, added, modified := compareUserList(users, users)

	if len(deleted)+len(added)+len(modified) != 0 {
		t.Fatalf("无变化时不该有任何分类，deleted=%d added=%d modified=%d",
			len(deleted), len(added), len(modified))
	}
}
