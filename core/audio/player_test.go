package audio

import (
	"errors"
	"sync"
	"testing"
)

// player_test.go：播放服务验收（冻结实现 player_service.py）。
//   - 限次播放：第 MaxPlays+1 次被拒（403 语义），increment-then-check；
//   - (audio_id, session_id) 维度隔离；store 可注入；
//   - 播放清单/顺序/倍速纯数据面（构造期校验 + 防御性拷贝）；
//   - 并发原子性（-race 绿）。

func TestPlayLimitTwoPlaysThenReject(t *testing.T) {
	store := NewInMemoryPlayCountStore()

	r1, err := Play("a1", "sess-1", "http://x/a1.mp3", store, MaxPlays)
	if err != nil || r1.PlayCount != 1 || r1.Remaining != 1 || r1.MaxPlays != MaxPlays {
		t.Fatalf("第 1 次播放分歧：r=%+v err=%v", r1, err)
	}
	if r1.URL != "http://x/a1.mp3" || r1.AudioID != "a1" || r1.SessionID != "sess-1" {
		t.Fatalf("透传字段分歧：%+v", r1)
	}
	r2, err := Play("a1", "sess-1", "http://x/a1.mp3", store, MaxPlays)
	if err != nil || r2.PlayCount != 2 || r2.Remaining != 0 {
		t.Fatalf("第 2 次播放分歧：r=%+v err=%v", r2, err)
	}

	// 第 3 次：increment-then-check → 拒绝且计数保留（记录尝试次数）.
	_, err = Play("a1", "sess-1", "http://x/a1.mp3", store, MaxPlays)
	if !errors.Is(err, ErrPlayLimitExceeded) {
		t.Fatalf("第 3 次必须被拒：got=%v", err)
	}
	var perr *PlayLimitError
	if !errors.As(err, &perr) {
		t.Fatalf("必须可取结构化明细：got=%v", err)
	}
	if perr.PlayCount != 3 || perr.MaxPlays != MaxPlays || perr.AudioID != "a1" || perr.SessionID != "sess-1" {
		t.Fatalf("超限明细分歧：%+v", perr)
	}
	if got := store.GetCount("a1", "sess-1"); got != 3 {
		t.Fatalf("尝试次数必须入账：got=%d", got)
	}
	// 第 4 次同样被拒.
	if _, err := Play("a1", "sess-1", "http://x/a1.mp3", store, MaxPlays); !errors.Is(err, ErrPlayLimitExceeded) {
		t.Fatalf("第 4 次必须同样被拒：got=%v", err)
	}
}

func TestPlaySessionAndAudioIsolation(t *testing.T) {
	store := NewInMemoryPlayCountStore()
	if _, err := Play("a1", "s1", "u", store, MaxPlays); err != nil {
		t.Fatalf("play: %v", err)
	}
	if _, err := Play("a1", "s1", "u", store, MaxPlays); err != nil {
		t.Fatalf("play: %v", err)
	}
	// 换 session：不受其他 session 计数影响.
	if _, err := Play("a1", "s2", "u", store, MaxPlays); err != nil {
		t.Fatalf("跨 session 计数不得串账：got=%v", err)
	}
	// 换音频：同 session 独立计数.
	if _, err := Play("a2", "s1", "u", store, MaxPlays); err != nil {
		t.Fatalf("跨音频计数不得串账：got=%v", err)
	}
	if got := store.GetCount("a1", "s2"); got != 1 {
		t.Fatalf("s2 计数分歧：%d", got)
	}
	// Reset 隔离面（测试隔离用）.
	store.Reset("a1", "s1")
	if got := store.GetCount("a1", "s1"); got != 0 {
		t.Fatalf("Reset 后必须为 0：got=%d", got)
	}
	if _, err := Play("a1", "s1", "u", store, MaxPlays); err != nil {
		t.Fatalf("Reset 后必须可重播：got=%v", err)
	}
}

func TestPlayCustomMaxPlaysAndValidation(t *testing.T) {
	store := NewInMemoryPlayCountStore()
	if _, err := Play("a", "s", "u", store, 1); err != nil {
		t.Fatalf("play: %v", err)
	}
	if _, err := Play("a", "s", "u", store, 1); !errors.Is(err, ErrPlayLimitExceeded) {
		t.Fatalf("max_plays=1 时第 2 次必须被拒：got=%v", err)
	}
	if _, err := Play("a", "s", "u", nil, MaxPlays); !errors.Is(err, ErrNilPlayStore) {
		t.Fatalf("nil store 必须报 ErrNilPlayStore：got=%v", err)
	}
	if _, err := Play("a", "s", "u", store, 0); !errors.Is(err, ErrInvalidMaxPlays) {
		t.Fatalf("max_plays=0 必须报 ErrInvalidMaxPlays：got=%v", err)
	}
	if _, err := Play("", "s", "u", store, MaxPlays); !errors.Is(err, ErrInvalidPlayArgs) {
		t.Fatalf("空 audio_id 必须报 ErrInvalidPlayArgs：got=%v", err)
	}
	if _, err := Play("a", "", "u", store, MaxPlays); !errors.Is(err, ErrInvalidPlayArgs) {
		t.Fatalf("空 session_id 必须报 ErrInvalidPlayArgs：got=%v", err)
	}
}

func TestPlayConcurrentAtomicLimit(t *testing.T) {
	// 并发原子性：max_plays=5，16 并发 → 恰好 5 成功（increment 原子面）.
	store := NewInMemoryPlayCountStore()
	const total, limit = 16, 5
	var mu sync.Mutex
	success := 0
	var wg sync.WaitGroup
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := Play("a", "s", "u", store, limit); err == nil {
				mu.Lock()
				success++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if success != limit {
		t.Fatalf("并发限次失准：成功 %d 次，期望 %d", success, limit)
	}
	if got := store.GetCount("a", "s"); got != total {
		t.Fatalf("全部尝试必须入账：got=%d", got)
	}
}

func TestNewPlaylistValidationAndClone(t *testing.T) {
	entries := []PlaylistEntry{{AudioID: "a1", AudioURL: "u1"}, {AudioID: "a2", AudioURL: "u2"}}
	pl, err := NewPlaylist("sess-9", entries, PlaybackParams{SpeedPct: 150})
	if err != nil {
		t.Fatalf("NewPlaylist: %v", err)
	}
	if pl.SessionID != "sess-9" || len(pl.Entries) != 2 || pl.Params.SpeedPct != 150 {
		t.Fatalf("清单字段分歧：%+v", pl)
	}
	// 顺序即播放序（构造序保持）.
	if pl.Entries[0].AudioID != "a1" || pl.Entries[1].AudioID != "a2" {
		t.Fatalf("清单顺序分歧：%+v", pl.Entries)
	}
	// 防御性拷贝：外部改写不污染清单.
	entries[0].AudioID = "hacked"
	if pl.Entries[0].AudioID == "hacked" {
		t.Fatal("清单与输入切片存在别名共享")
	}

	for _, tc := range []struct {
		name    string
		session string
		entries []PlaylistEntry
		params  PlaybackParams
	}{
		{"空 session", "", entries, PlaybackParams{SpeedPct: 100}},
		{"空条目", "s", nil, PlaybackParams{SpeedPct: 100}},
		{"条目缺 audio_id", "s", []PlaylistEntry{{AudioID: "", AudioURL: "u"}}, PlaybackParams{SpeedPct: 100}},
		{"条目缺 url", "s", []PlaylistEntry{{AudioID: "a", AudioURL: ""}}, PlaybackParams{SpeedPct: 100}},
		{"非正倍速", "s", entries, PlaybackParams{SpeedPct: 0}},
	} {
		if _, err := NewPlaylist(tc.session, tc.entries, tc.params); !errors.Is(err, ErrInvalidPlaylist) {
			t.Fatalf("%s 必须报 ErrInvalidPlaylist：got=%v", tc.name, err)
		}
	}
}
