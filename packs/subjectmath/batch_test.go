package subjectmath

import (
	"fmt"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// batch_test.go：批量管线的红线机制——
//   - 参数空间不足 N 即拒绝（唯一率不可达就不许开跑）；
//   - content 摘要碰撞必须整批硬失败（H-W6-1 负例：参数折叠必然暴露）；
//   - 配额未达成返回错误且拒绝分布留痕；
//   - Run 对未注册模板 id 拒绝。

// dupPairGen 把整个参数空间折叠到两个身体（偶数索引→inner[1]，奇数→inner[0]）
// ——人为注入“参数折叠”，第 3 次尝试内必然发生 content 摘要碰撞，
// 验证批量层绝不静默放行重复内容。
type dupPairGen struct{ inner Generator }

func (d *dupPairGen) Entry() registry.Entry { return d.inner.Entry() }
func (d *dupPairGen) Spec() map[string]any  { return d.inner.Spec() }
func (d *dupPairGen) Size() int             { return d.inner.Size() }
func (d *dupPairGen) Instance(i int) (*Instance, error) {
	if i%2 == 1 {
		return d.inner.Instance(0)
	}
	return d.inner.Instance(1)
}

// alienGen 产出永远过不了验证器的异类实例（TemplateID 未注册）。
type alienGen struct{}

func (alienGen) Entry() registry.Entry { return registry.Entry{ID: "tpl-fake-alien", Version: "0"} }
func (alienGen) Spec() map[string]any  { return map[string]any{"template_id": "tpl-fake-alien"} }
func (alienGen) Size() int             { return 100 }
func (alienGen) Instance(i int) (*Instance, error) {
	inner, ok := Get(idIntMul)
	if !ok {
		return nil, fmt.Errorf("intmul 未注册")
	}
	inst, err := inner.Instance(0)
	if err != nil {
		return nil, err
	}
	inst.TemplateID = "tpl-not-registered-anywhere"
	return inst, nil
}

func TestRunRejectsUnknownTemplate(t *testing.T) {
	if _, _, err := Run(Options{TemplateID: "no-such-template", N: 5, Seed: 1}); err == nil {
		t.Fatal("未知模板必须报错")
	}
}

func TestBatchRejectsSpaceSmallerThanN(t *testing.T) {
	g, _ := Get(idFracCmp)
	n := g.Size() + 1
	_, _, err := runBatch(g, Options{TemplateID: idFracCmp, N: n, Seed: 1})
	if err == nil || !strings.Contains(err.Error(), "结构互异不可达") {
		t.Fatalf("空间(%d) < N(%d) 必须前置拒绝，得 %v", g.Size(), n, err)
	}
}

// 红线负例：即使有人把生成器写坏造成内容折叠，批量层也必须整批失败，
// 唯一率 100% 是断言出来的事实而非宣称。
func TestBatchHardFailsOnDigestCollision(t *testing.T) {
	g, _ := Get(idIntMul)
	recs, rep, err := runBatch(&dupPairGen{inner: g}, Options{TemplateID: idIntMul, N: 30, Seed: 9})
	if err == nil {
		t.Fatal("注入折叠后批次应失败")
	}
	if !strings.Contains(err.Error(), "H-W6-1") || !strings.Contains(err.Error(), "摘要相同") {
		t.Fatalf("碰撞错误信息应指向 H-W6-1 口径: %v", err)
	}
	if len(recs) != 0 && rep == nil {
		t.Fatal("碰撞错误也应带回部分报告")
	}
}

func TestBatchReportsQuotaUnmetWithRejectionDistribution(t *testing.T) {
	recs, rep, err := runBatch(alienGen{}, Options{TemplateID: "tpl-fake-alien", N: 3, Seed: 2})
	if err == nil {
		t.Fatal("全部被验证器拒绝时配额必须不达成")
	}
	if !strings.Contains(err.Error(), "配额未达成") {
		t.Fatalf("错误应说明配额未达成: %v", err)
	}
	if rep == nil || rep.Accepted != 0 {
		t.Fatalf("部分报告应带回且合格数为 0: %+v", rep)
	}
	totalRej := 0
	for _, c := range rep.Rejected {
		totalRej += c
	}
	if totalRej == 0 {
		t.Fatal("拒绝分布必须留痕")
	}
	foundShape := false
	for k := range rep.Rejected {
		if strings.HasPrefix(k, "validate:") {
			foundShape = true
		}
	}
	if !foundShape {
		t.Fatalf("拒绝分布应含验证器类别: %v", rep.Rejected)
	}
	if len(recs) != 0 {
		t.Fatalf("不应有记录流出: %d", len(recs))
	}
}

// N 为零/负的前置防御。
func TestBatchRejectsNonPositiveN(t *testing.T) {
	g, _ := Get(idIntMul)
	if _, _, err := runBatch(g, Options{TemplateID: idIntMul, N: 0, Seed: 1}); err == nil ||
		!strings.Contains(fmt.Sprint(err), "N") {
		t.Fatalf("N<=0 必须拒: %v", err)
	}
}
