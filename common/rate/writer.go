package rate

import (
	"sync/atomic"
	"time"

	"github.com/juju/ratelimit"
	"github.com/xtls/xray-core/common"
	"github.com/xtls/xray-core/common/buf"
)

// DynamicBucket 是可以在线改速率的令牌桶。
//
// 老实现把 *ratelimit.Bucket 直接绑在连接 / writer 上，用户限速一变就只能
// 重建连接才能换桶——这正是「改个限速就断流」的来源。这里用 atomic.Value
// 持有桶，Update 时原地换一个新桶，已经建立的连接下一次 Wait 就按新速率走，
// 连接本身不受影响。
type DynamicBucket struct {
	v atomic.Value // *ratelimit.Bucket
}

func NewDynamicBucket(rate int64) *DynamicBucket {
	d := &DynamicBucket{}
	d.v.Store(ratelimit.NewBucketWithQuantum(time.Second, rate, rate))
	return d
}

// Get 返回当前生效的桶；未初始化时返回 nil，调用方需判空。
func (d *DynamicBucket) Get() *ratelimit.Bucket {
	if d == nil {
		return nil
	}
	b, _ := d.v.Load().(*ratelimit.Bucket)
	return b
}

// Update 用新速率替换桶，不阻塞、不影响已建立的连接。
func (d *DynamicBucket) Update(rate int64) {
	d.v.Store(ratelimit.NewBucketWithQuantum(time.Second, rate, rate))
}

type Writer struct {
	writer  buf.Writer
	limiter *DynamicBucket
}

func NewRateLimitWriter(writer buf.Writer, limiter *DynamicBucket) buf.Writer {
	return &Writer{
		writer:  writer,
		limiter: limiter,
	}
}

func (w *Writer) Close() error {
	return common.Close(w.writer)
}

func (w *Writer) WriteMultiBuffer(mb buf.MultiBuffer) error {
	if l := w.limiter.Get(); l != nil {
		l.Wait(int64(mb.Len()))
	}
	return w.writer.WriteMultiBuffer(mb)
}
