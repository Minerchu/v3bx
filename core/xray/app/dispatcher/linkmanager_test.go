package dispatcher

import (
	"sync"
	"testing"

	"github.com/xtls/xray-core/common/buf"
)

type nopWriter struct{}

func (nopWriter) WriteMultiBuffer(mb buf.MultiBuffer) error { return nil }

type nopReader struct{}

func (nopReader) ReadMultiBuffer() (buf.MultiBuffer, error) { return nil, nil }

// TestLinkManagerConcurrentCloseAll 回归测试：AddLink 与 CloseAll 并发时不能 panic。
//
// 老实现 CloseAll 无锁遍历 map，循环体里的 common.Close(w) 会回调
// ManagedWriter.Close -> RemoveWriter -> delete(m.links, ...)，
// 等于边遍历边写 map，Go runtime 会 fatal error:
// "concurrent map iteration and map write"，整个进程连带所有节点一起挂。
//
// 用 -race 跑这个测试能同时验证数据竞争。
func TestLinkManagerConcurrentCloseAll(t *testing.T) {
	for round := 0; round < 200; round++ {
		lm := &LinkManager{links: make(map[*ManagedWriter]buf.Reader)}

		var wg sync.WaitGroup
		start := make(chan struct{})

		// 模拟多连接同时建立
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				for j := 0; j < 20; j++ {
					w := &ManagedWriter{writer: nopWriter{}, manager: lm}
					lm.AddLink(w, nopReader{})
				}
			}()
		}

		// 同时踢人
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			lm.CloseAll()
			lm.CloseAll() // 幂等，重复调用不应 panic
		}()

		close(start)
		wg.Wait()
	}
}

// TestLinkManagerAddAfterClose CloseAll 之后再有连接进来，不应被记录。
func TestLinkManagerAddAfterClose(t *testing.T) {
	lm := &LinkManager{links: make(map[*ManagedWriter]buf.Reader)}
	lm.CloseAll()

	w := &ManagedWriter{writer: nopWriter{}, manager: lm}
	lm.AddLink(w, nopReader{})

	lm.mu.Lock()
	n := len(lm.links)
	lm.mu.Unlock()
	if n != 0 {
		t.Fatalf("CloseAll 之后不该再记录链路，实际 %d 条", n)
	}
}
