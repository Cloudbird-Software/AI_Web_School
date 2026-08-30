// papers.go 承载组卷/出题 HTTP 端点接线（审计卡 #148）：POST /papers、
// GET /papers/{paper_id}、POST /generate 三端的协议面。
//
// 分层纪律（api/api.go 同款）：本文件只做协议层——认证盾（staffOrOps）→
// 请求解码 → core/assembly 编排（或出题批量管线）→ 契约 JSON 直出/脱敏
// 错误映射。组卷业务语义零新增：编译/装载/曝光过滤/求解/渲染全链在
// core/assembly.Orchestrate（#147），本层只做蓝图解码与错误归类。
//
// 注入接缝（consumer-side，ContentQueries 同一纪律）：PapersWiring 聚合
// 编排器、制品存储两个接缝；候选题源（Orchestrator.Source）或制品存储
// 未注入时对应端点保持 501 占位（装配语义，认证盾照挂；fail-closed 同
// X12——查询面未接线绝不回伪造/空数据）。
//
// 制品存储：PaperArtifactStore 端口 + 进程内 Memory 实现（服务重启即失，
// 如实语义）。PG 落库挂后续卡：卷写入必须与事务性曝光预留同事务提交
// （core/assembly/exposure.go 的事务边界论证），不属于本卡「编排 +
// 内存回读」的最小闭环面。
package api

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"sync"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/assembly"
	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectmath"
)

// ErrUnknownPaper 是制品账面无行的哨兵（→404 的唯一映射面；与
// content.ErrUnknown* 同构的显式哨兵，不做字符串匹配）.
var ErrUnknownPaper = errors.New("api: paper 制品不存在")

// PaperArtifactStore 是试卷制品的存储端口（consumer-side）：编排成功落
// 制品、按 paper_id 回读。PG 实现挂后续卡（见文件头注释），本卡生产形态
// 为进程内 Memory（装配方持有生命周期）。
type PaperArtifactStore interface {
	// Save 落一份制品（paper_id 冲突 = 同内容重生成，覆盖为幂等语义）.
	Save(ctx context.Context, art *assembly.PaperArtifact) error
	// Get 按 paper_id 回读；账面无行返回 ErrUnknownPaper.
	Get(ctx context.Context, paperID string) (*assembly.PaperArtifact, error)
}

// MemoryPaperArtifacts 是 PaperArtifactStore 的进程内实现：互斥锁保护的
// id 索引表，服务重启即失（如实语义，不冒充持久化）。多副本部署必须等
// PG 落库卡。
type MemoryPaperArtifacts struct {
	mu        sync.Mutex
	artifacts map[string]*assembly.PaperArtifact
}

// NewMemoryPaperArtifacts 构造空制品账.
func NewMemoryPaperArtifacts() *MemoryPaperArtifacts {
	return &MemoryPaperArtifacts{artifacts: map[string]*assembly.PaperArtifact{}}
}

// Save 落制品（幂等覆盖）.
func (m *MemoryPaperArtifacts) Save(_ context.Context, art *assembly.PaperArtifact) error {
	if art == nil {
		return errors.New("api: 拒绝落空制品")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.artifacts == nil {
		m.artifacts = map[string]*assembly.PaperArtifact{}
	}
	m.artifacts[art.Metadata.PaperID] = art
	return nil
}

// Get 按 paper_id 回读；无行返回 ErrUnknownPaper.
func (m *MemoryPaperArtifacts) Get(_ context.Context, paperID string) (*assembly.PaperArtifact, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	art, ok := m.artifacts[paperID]
	if !ok {
		return nil, ErrUnknownPaper
	}
	return art, nil
}

// PapersWiring 聚合组卷/出题端点的注入接缝（nil 字段 = 对应端点 501 占位
// 或走默认管线，见各字段）.
type PapersWiring struct {
	// Orchestrator 组卷编排器（#147）；Source 为 nil 时 Orchestrate 自身
	// fail-closed，本层视同「候选面未注入」保持 501 占位.
	Orchestrator *assembly.Orchestrator
	// Artifacts 制品存储（Memory 生产形态；PG 挂后续卡）.
	Artifacts PaperArtifactStore
	// Generator 出题管线接缝（nil = 默认绑定 defaultDraftGenerator，即
	// packs/subjectmath.Run；测试注入 fake 全链隔离）.
	Generator DraftGenerator
}

// DraftGenerator 是出题批量管线的消费侧接缝（签名对齐 packs/subjectmath.Run）：
// api 协议面零学科语义——无按学科分支、响应键面（generated/accepted/digests）
// 与学科无关；学科包仅作为装配期的默认管线绑定在此接缝上。X6 边界的执行面
// 是 GO-3 lint（core/ 不 import packs/），api 不在其列；本端点为教研/运维侧
// 的出题产能工具端点（#148 卡面指定 subjectmath 管线）。
type DraftGenerator func(templateID string, n int, seed uint64) (generated, accepted int, digests []string, err error)

// defaultDraftGenerator 生产默认管线：packs/subjectmath.Run 批量出题
// （确定性：同 (template_id, n, seed) 同输出；互异/验证器语义在包内红线
// 断言）。错误一并透传给协议面归类。
func defaultDraftGenerator(templateID string, n int, seed uint64) (int, int, []string, error) {
	records, rep, err := subjectmath.Run(subjectmath.Options{TemplateID: templateID, N: n, Seed: seed})
	if err != nil || rep == nil {
		return 0, 0, nil, err
	}
	digests := make([]string, 0, len(records))
	for i := range records {
		digests = append(digests, records[i].ContentDigest)
	}
	return rep.Generated, rep.Accepted, digests, nil
}

// papersHandlers 返回三条组卷/出题端点的 handler（create / read / generate）。
// 依赖未注入的端点保持 501 占位（装配语义，认证盾照挂）；逐端点的业务
// 接线见下方各 handler。
func papersHandlers(papers PapersWiring) (create, read, generate http.HandlerFunc) {
	create, read, generate = notImplemented, notImplemented, notImplemented
	// 候选题源未注入（编排器为 nil 或其 Source 为 nil）= DB 候选面未接线：
	// 保持 501 占位——编排自身对 nil Source 的 fail-closed 在协议面之前拦截
	// （装配语义显式暴露，而非混在 500 里）。制品账同理：编排成功但无处
	// 落账的调用必 500，装配期就不该放行.
	if papers.Orchestrator != nil && papers.Orchestrator.Source != nil && papers.Artifacts != nil {
		create = createPaper(papers)
	}
	if papers.Artifacts != nil {
		read = readPaper(papers.Artifacts)
	}
	// POST /generate 无注入前置（Generator nil = 默认 subjectmath 管线）.
	generate = generateDraftItems(papers)
	return create, read, generate
}

// createPaper 是 POST /papers 的业务接线（#148 端点 1）：蓝图 JSON 解码 →
// core/assembly.Orchestrate 全链（编译→装载→曝光过滤→求解→渲染）→ 制品
// 落账 → 201 卷元数据。业务语义零新增（编排层 #147 的契约直通）。
//
// 错误映射（脱敏单一出口 writeErrorClass）：
//   - 请求体非合法 JSON / assembly.ErrInvalidBlueprint → 422 bad_request
//     （调用方可修的输入违例）；
//   - 编排其余失败（候选池故障/坏行/不可行报告/渲染失败）→ 500 internal
//     单字段脱敏——结构化细节（InfeasibleError 冲突清单等）只进服务端日志，
//     绝不外泄消息/驱动细节；
//   - 制品落账失败 → 500 internal（编排成功但账不可写，不回伪造 201）.
func createPaper(papers PapersWiring) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var bp assembly.PaperBlueprint
		if err := decodeJSONBody(r, &bp); err != nil {
			writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
			return
		}
		art, err := papers.Orchestrator.Orchestrate(r.Context(), bp, assembly.OrchestrateOptions{})
		if err != nil {
			if errors.Is(err, assembly.ErrInvalidBlueprint) {
				writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
				return
			}
			// 日志只落 route pattern（服务端路由表常量）——r.URL.Path 是请求
			// 方可控字节，落日志即注入面（go/log-injection，contentRead 同款）.
			log.Printf("paper orchestration failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
			return
		}
		if err := papers.Artifacts.Save(r.Context(), art); err != nil {
			log.Printf("paper persist failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		if encErr := json.NewEncoder(w).Encode(art.Metadata); encErr != nil {
			log.Printf("paper metadata encode failure error_class=%T", encErr)
		}
	}
}

// readPaper 是 GET /papers/{paper_id} 的业务接线（#148 端点 2）：制品账
// 按 paper_id 回读直出。
//
// 响应面：200 PaperArtifact JSON——metadata + qr + html。HTML []byte 按
// encoding/json 惯例以 base64 承载：本端点面向机器审计与再消费（制品的
// 内容寻址可回放面），非浏览器直出；卷面 HTML 的人工预览走 cmd/papergen
// 的 -out 产物文件。
//
// 错误映射：ErrUnknownPaper → 404 not_found（账面无行）；存储驱动故障 →
// 500 internal 单字段脱敏.
func readPaper(store PaperArtifactStore) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		art, err := store.Get(r.Context(), r.PathValue("paper_id"))
		if err != nil {
			if errors.Is(err, ErrUnknownPaper) {
				writeErrorClass(w, http.StatusNotFound, middleware.ErrorClassNotFound)
				return
			}
			log.Printf("paper read failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if encErr := json.NewEncoder(w).Encode(art); encErr != nil {
			log.Printf("paper artifact encode failure error_class=%T", encErr)
		}
	}
}

// generateRequest 是 POST /generate 的请求体（出题产能面：母题 × 数量 × 种子）.
type generateRequest struct {
	TemplateID string `json:"template_id"`
	N          int    `json:"n"`
	Seed       uint64 `json:"seed"`
}

// generateResponse 是 POST /generate 的 200 响应体（三键最小回执面）.
type generateResponse struct {
	Generated int      `json:"generated"`
	Accepted  int      `json:"accepted"`
	Digests   []string `json:"digests"`
}

// generateDraftItems 是 POST /generate 的业务接线（#148 端点 3）：
// packs/subjectmath.Run 确定性出题 → 200 {generated, accepted, digests}
// （digests 为合格实例的 content 摘要，按生成序）。
//
// 产物语义（如实声明，不伪造入账）：本端点产物是草稿——subjectmath 批量
// 管线的合格实例不入内容账（item_version 账面零写入、无门证书、无
// published 状态迁移），入账必须走 cmd/ingest 的既有发布链。回执只是草稿
// 摘要，供教研/运维核对产能与互异面。
//
// 错误映射：请求体非 JSON / template_id 缺失 / n 非正 → 422 bad_request；
// 批量管线的确定性拒绝（未知母题/参数空间不足/配额未达成）同为调用方可修
// 的输入违例 → 422（重试同参无意义；拒绝分布属生成器内部细节，只进服务端
// 日志）.
func generateDraftItems(papers PapersWiring) http.HandlerFunc {
	gen := papers.Generator
	if gen == nil {
		gen = defaultDraftGenerator
	}
	return func(w http.ResponseWriter, r *http.Request) {
		var req generateRequest
		if err := decodeJSONBody(r, &req); err != nil || req.TemplateID == "" || req.N <= 0 {
			writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
			return
		}
		generated, accepted, digests, err := gen(req.TemplateID, req.N, req.Seed)
		if err != nil {
			log.Printf("generate failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusUnprocessableEntity, middleware.ErrorClassBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if encErr := json.NewEncoder(w).Encode(generateResponse{
			Generated: generated,
			Accepted:  accepted,
			Digests:   digests,
		}); encErr != nil {
			log.Printf("generate response encode failure error_class=%T", encErr)
		}
	}
}

// decodeJSONBody 解码 JSON 请求体（体限已在边界层收口）。空体/读失败/语法
// 错归一为协议违例（→422），不给半解析态进入业务路径的机会.
func decodeJSONBody(r *http.Request, v any) error {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxPlaceholderBodyBytes+1))
	if err != nil {
		return err
	}
	if len(body) == 0 {
		return errors.New("api: 请求体为空")
	}
	return json.Unmarshal(body, v)
}
