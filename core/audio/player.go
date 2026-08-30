package audio

import (
	"errors"
	"fmt"
	"sync"
)

// player.go 承载在线播放器服务（冻结实现 src/core/audio/player_service.py
// T-W4-024 的 Go 重锚定，架构 v2 §4.6 / S5）+ 播放清单数据面。
//
// 限次播放（ADR §4.6「模拟考试只听 1–2 遍」）：播放次数按 (audio_id,
// session_id) 维度计数，同一 session 内同一音频累计播放；MaxPlays 是允许的
// 成功播放次数上限（默认 2），第 MaxPlays+1 次调用被拒（API 层转 403）。
//
// 为什么 increment-then-check 而非 check-then-increment：increment 是原子
// 操作，避免并发场景下的 check-then-act 竞态。第 3 次调用 increment → 3 > 2
// → 拒绝，计数保持 3（记录尝试次数，后续调用同样被拒）。
//
// 为什么用可注入的 PlayCountStore：生产环境用 Redis（原子 INCR + TTL），
// 单元测试用 InMemoryPlayCountStore（确定性、无外部依赖）——与
// AudioStorageWriter / TTSEngine 同模式的副作用边界。与冻结实现的显式差异：
// Python 持模块级默认 store 单例；Go 侧 store 必填（显式注入，跨测试无隐式
// 耦合），nil 即编码面错误 fail-loud。
//
// 宪法 A5/X6：不 import 学科包/学段包。宪法 D7：audio_url 由上层调用方从
// AudioAsset.URL 传入，本包不感知 PII。

// MaxPlays 最大允许播放次数（第 MaxPlays+1 次调用被拒；ADR §4.6）.
const MaxPlays = 2

// 哨兵错误：调用方按 errors.Is 分支处理.
var (
	// ErrPlayLimitExceeded 表示播放次数超限（errors.As 可取 *PlayLimitError
	// 明细；API 层转 403 Forbidden）.
	ErrPlayLimitExceeded = errors.New("audio: 播放次数超限")
	// ErrNilPlayStore 表示未注入播放计数存储.
	ErrNilPlayStore = errors.New("audio: 播放计数 store 不可为 nil")
	// ErrInvalidMaxPlays 表示 maxPlays 非法（必须 >= 1）.
	ErrInvalidMaxPlays = errors.New("audio: max_plays 非法（必须 >= 1）")
	// ErrInvalidPlayArgs 表示播放入参非法（audio_id/session_id 为空——计数
	// 维度退化，fail-loud）.
	ErrInvalidPlayArgs = errors.New("audio: 播放入参非法")
	// ErrInvalidPlaylist 表示播放清单构造参数非法.
	ErrInvalidPlaylist = errors.New("audio: 播放清单非法")
)

// PlayLimitError 是播放超限的结构化错误（PlayLimitExceededError 对齐）.
type PlayLimitError struct {
	AudioID   string
	SessionID string
	PlayCount int
	MaxPlays  int
}

// Error 实现 error.
func (e *PlayLimitError) Error() string {
	return fmt.Sprintf("播放超限：audio_id=%q session_id=%q 已播放 %d 次（上限 %d 次）",
		e.AudioID, e.SessionID, e.PlayCount, e.MaxPlays)
}

// Is 使 errors.Is(err, ErrPlayLimitExceeded) 成立.
func (e *PlayLimitError) Is(target error) bool { return target == ErrPlayLimitExceeded }

// PlayCountStore 是播放次数存储契约（生产替换为 Redis 适配器）。
//
// 语义：Increment 是原子的（并发安全），返回递增后的新计数。
// 生产 Redis 实现：INCR key + EXPIRE key ttl（session 级 TTL）.
type PlayCountStore interface {
	// Increment 原子递增 (audioID, sessionID) 的播放计数，返回新值.
	Increment(audioID, sessionID string) int
	// GetCount 获取当前播放计数（不递增）.
	GetCount(audioID, sessionID string) int
	// Reset 重置计数（测试隔离用）.
	Reset(audioID, sessionID string)
}

// InMemoryPlayCountStore 内存播放次数存储（测试用，生产替换为 Redis）。
// 并发安全（内置互斥锁）.
type InMemoryPlayCountStore struct {
	mu     sync.Mutex
	counts map[string]int
}

// NewInMemoryPlayCountStore 构造内存存储.
func NewInMemoryPlayCountStore() *InMemoryPlayCountStore {
	return &InMemoryPlayCountStore{counts: make(map[string]int)}
}

func playStoreKey(audioID, sessionID string) string { return audioID + "\x00" + sessionID }

// Increment 实现 PlayCountStore（原子面=锁内递增）.
func (s *InMemoryPlayCountStore) Increment(audioID, sessionID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := playStoreKey(audioID, sessionID)
	s.counts[k]++
	return s.counts[k]
}

// GetCount 实现 PlayCountStore.
func (s *InMemoryPlayCountStore) GetCount(audioID, sessionID string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.counts[playStoreKey(audioID, sessionID)]
}

// Reset 实现 PlayCountStore.
func (s *InMemoryPlayCountStore) Reset(audioID, sessionID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.counts, playStoreKey(audioID, sessionID))
}

// PlayResult 是播放结果（验收 #1）.
type PlayResult struct {
	// AudioID / SessionID 播放维度标识.
	AudioID   string
	SessionID string
	// URL 音频可访问 URL（来自 AudioAsset.URL，消费层透传）.
	URL string
	// PlayCount 本次播放后的累计次数（含本次；超限尝试也计数）.
	PlayCount int
	// MaxPlays 允许的最大播放次数.
	MaxPlays int
	// Remaining 剩余播放次数（MaxPlays - PlayCount）.
	Remaining int
}

// Play 播放音频：返回音频 URL，同一 session 第 maxPlays+1 次调用被拒
// （验收 #1）。maxPlays 传 <=0 视为编码面错误（显式传 MaxPlays 即默认值）。
// audioID/sessionID 为空属调用方编码面错误：计数维度退化为全局键，本面按
// fail-loud 拒绝（空 audioID 无法定位音频）.
func Play(audioID, sessionID, audioURL string, store PlayCountStore, maxPlays int) (*PlayResult, error) {
	if store == nil {
		return nil, ErrNilPlayStore
	}
	if audioID == "" || sessionID == "" {
		return nil, fmt.Errorf("%w: audio_id/session_id 不能为空", ErrInvalidPlayArgs)
	}
	if maxPlays < 1 {
		return nil, fmt.Errorf("%w: 得到 %d", ErrInvalidMaxPlays, maxPlays)
	}
	newCount := store.Increment(audioID, sessionID)
	if newCount > maxPlays {
		return nil, &PlayLimitError{
			AudioID:   audioID,
			SessionID: sessionID,
			PlayCount: newCount,
			MaxPlays:  maxPlays,
		}
	}
	return &PlayResult{
		AudioID:   audioID,
		SessionID: sessionID,
		URL:       audioURL,
		PlayCount: newCount,
		MaxPlays:  maxPlays,
		Remaining: maxPlays - newCount,
	}, nil
}

// PlaylistEntry 是播放清单条目（顺序即卷面播放序；纯数据面）.
type PlaylistEntry struct {
	AudioID  string
	AudioURL string
}

// PlaybackParams 是播放参数：倍速百分比（100=原速）。策略区间（如 0.5x–2x）
// 属学段/产品配置面，本数据面只拒绝非正值（编码面错误 fail-loud）.
type PlaybackParams struct {
	SpeedPct int
}

// Playlist 是播放清单（纯数据面：清单/顺序/倍速参数）。
// Entries 顺序即播放顺序（构造序，深拷贝保存——外部改写不污染清单）.
type Playlist struct {
	SessionID string
	Entries   []PlaylistEntry
	Params    PlaybackParams
}

// NewPlaylist 构造并校验播放清单（pydantic 构造期校验的 Go 对齐面）：
// session 非空、条目非空、每条 audio_id/audio_url 非空、倍速为正.
func NewPlaylist(sessionID string, entries []PlaylistEntry, params PlaybackParams) (*Playlist, error) {
	if sessionID == "" {
		return nil, fmt.Errorf("%w: session_id 不能为空", ErrInvalidPlaylist)
	}
	if len(entries) == 0 {
		return nil, fmt.Errorf("%w: entries 不能为空", ErrInvalidPlaylist)
	}
	for i, e := range entries {
		if e.AudioID == "" || e.AudioURL == "" {
			return nil, fmt.Errorf("%w: entries[%d] audio_id/audio_url 不能为空", ErrInvalidPlaylist, i)
		}
	}
	if params.SpeedPct <= 0 {
		return nil, fmt.Errorf("%w: SpeedPct=%d 非法（必须 > 0）", ErrInvalidPlaylist, params.SpeedPct)
	}
	cloned := make([]PlaylistEntry, len(entries))
	copy(cloned, entries)
	return &Playlist{SessionID: sessionID, Entries: cloned, Params: params}, nil
}
