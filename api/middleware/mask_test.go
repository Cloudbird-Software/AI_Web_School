package middleware

// T-W5-007 屏蔽矩阵（三出口）：已登记凭证值/敏感键值对从任何出口离开进程
// 前必须被打码——
//
//	出口 1 响应体：HandleError 写出的 body 只含固定 error_class，零凭证残片；
//	出口 2 日志行：HandleError / Recover 落日志的动态文本已打码；
//	出口 3 错误链：错误文本（日志出口的输入侧）经 Mask 后零凭证残片。
//
// 测试凭证值刻意避开 X3 扫描形态（值内混入点号断开可匹配游程）。

import (
	"bytes"
	"encoding/json"
	"fmt"
	"go/ast"
	goparser "go/parser"
	"go/token"
	"io/fs"
	"log"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

const (
	rawGatewayKey  = "gw.raw.0123456789abcdef000001"
	rawSigningKey  = "sk.raw.0123456789abcdef000001"
	plainSensitive = "abcdef012345"
)

// maskTestRegistry 构造已加载双凭证并注入本包 mask 出口的登记面。
func maskTestRegistry(t *testing.T) {
	t.Helper()
	r := auth.NewCredentialRegistry()
	specs := []auth.CredentialSpec{
		{
			Name: "llm_gateway_key", EnvVar: "TEST_LLM_GATEWAY_KEY", Provider: "litellm-gateway",
			Class:  auth.ClassLLM,
			Loader: func() (auth.Secret, bool) { return auth.NewSecret(rawGatewayKey), true },
		},
		{
			Name: "signing_key", EnvVar: "TEST_SIGNING_KEY", Provider: "in-process HMAC signer",
			Class:  auth.ClassAuth,
			Loader: func() (auth.Secret, bool) { return auth.NewSecret(rawSigningKey), true },
		},
	}
	for _, spec := range specs {
		if err := r.Register(spec); err != nil {
			t.Fatalf("登记 %s 失败: %v", spec.Name, err)
		}
	}
	if _, err := r.Validate(); err != nil {
		t.Fatalf("校验失败: %v", err)
	}
	SetCredentialRegistry(r)
	t.Cleanup(func() { SetCredentialRegistry(nil) })
}

// captureLogs 捕获标准 log 输出（两个出口测试共用）。
func captureLogs(t *testing.T) func() string {
	t.Helper()
	var buf bytes.Buffer
	prev := log.Writer()
	prevFlags := log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	t.Cleanup(func() {
		log.SetOutput(prev)
		log.SetFlags(prevFlags)
	})
	return buf.String
}

// TestMaskExitsMatrix 验收：三出口屏蔽矩阵。
func TestMaskExitsMatrix(t *testing.T) {
	maskTestRegistry(t)

	// 泄漏源：模拟底层错误回显凭证值与敏感键值对（错误链入口）。
	leak := fmt.Errorf("upstream 500: endpoint %s rejected; sent header token:%s", rawGatewayKey, plainSensitive)

	t.Run("出口3_错误链", func(t *testing.T) {
		masked := Mask(leak.Error())
		if strings.Contains(masked, rawGatewayKey) || strings.Contains(masked, plainSensitive) {
			t.Fatalf("错误链出口泄漏: %q", masked)
		}
		if !strings.Contains(masked, auth.Masked) {
			t.Fatalf("打码结果应保留 *** 形态: %q", masked)
		}
	})

	t.Run("出口1_响应体与出口2_日志行", func(t *testing.T) {
		logs := captureLogs(t)
		rec := httptest.NewRecorder()
		HandleError(rec, leak)

		// 响应体：单字段脱敏体，零动态文本（凭证值/键值对残片均不存在）。
		var resp errorResponse
		if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
			t.Fatalf("响应体不是合法 JSON: %v", err)
		}
		if resp.ErrorClass != ErrorClassInternal {
			t.Fatalf("未分类泄漏错误应落 500 internal: %q", resp.ErrorClass)
		}
		body := rec.Body.String()
		if strings.Contains(body, rawGatewayKey) || strings.Contains(body, plainSensitive) {
			t.Fatalf("响应体出口泄漏: %s", body)
		}

		// 日志行：reason 保留可诊断信息但凭证已打码。
		out := logs()
		if strings.Contains(out, rawGatewayKey) || strings.Contains(out, plainSensitive) {
			t.Fatalf("日志出口泄漏: %q", out)
		}
		if !strings.Contains(out, auth.Masked) {
			t.Fatalf("日志应含打码形态: %q", out)
		}
	})

	t.Run("出口2_panic防线日志", func(t *testing.T) {
		logs := captureLogs(t)
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/panic", nil)
		Recover(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			panic(fmt.Errorf("credentials %s expired", rawSigningKey))
		})).ServeHTTP(rec, req)

		out := logs()
		if strings.Contains(out, rawSigningKey) {
			t.Fatalf("panic 日志出口泄漏: %q", out)
		}
		if !strings.Contains(out, auth.Masked) {
			t.Fatalf("panic 日志应含打码形态: %q", out)
		}
		// panic 防线的响应出口同样是单字段体。
		if rec.Code != http.StatusInternalServerError ||
			strings.Contains(rec.Body.String(), rawSigningKey) {
			t.Fatalf("panic 响应出口异常: %d %s", rec.Code, rec.Body.String())
		}
	})
}

// TestMaskWithoutRegistry 未注入登记面时 Mask 为恒等函数：mask 层缺席不
// 改变既有行为（防御纵深只增不改）。
func TestMaskWithoutRegistry(t *testing.T) {
	SetCredentialRegistry(nil)
	t.Cleanup(func() { SetCredentialRegistry(nil) })
	in := "ordinary text " + rawGatewayKey
	if Mask(in) != in {
		t.Fatalf("未注入登记面时 Mask 应恒等: %q", Mask(in))
	}
}

// TestResponseModelsContainNoCredentialFields 原卡验收 #4 的 Go 形态：
// 静态扫描 api/ 与 api/middleware/ 全部响应模型（带 json tag 的结构体字段），
// 断言字段名不含凭证类词根——凭证字段一旦出现在响应模型即测试红（对齐
// Python test_credential_leakage 的静态扫描语义；响应体脱敏另由出口矩阵
// 动态复核）。D9：服务端凭证永不经任何 API 回传。
func TestResponseModelsContainNoCredentialFields(t *testing.T) {
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("无法定位源文件目录")
	}
	dirs := []string{
		filepath.Join(filepath.Dir(thisFile), ".."), // api/ 响应模型
		filepath.Dir(thisFile),                      // api/middleware/ 边界响应体
	}
	reCredentialField := regexp.MustCompile(`(?i)(secret|password|token|api[_-]?key|credential)`)
	fset := token.NewFileSet()
	for _, dir := range dirs {
		err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				if d.Name() == "testdata" {
					return filepath.SkipDir
				}
				return nil
			}
			// 响应模型住在非测试源文件；测试文件里的夹具不构成对外出口。
			if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
				return nil
			}
			f, perr := goparser.ParseFile(fset, path, nil, 0)
			if perr != nil {
				return perr
			}
			for _, decl := range f.Decls {
				gd, isGen := decl.(*ast.GenDecl)
				if !isGen {
					continue
				}
				for _, spec := range gd.Specs {
					ts, isType := spec.(*ast.TypeSpec)
					if !isType {
						continue
					}
					st, isStruct := ts.Type.(*ast.StructType)
					if !isStruct {
						continue
					}
					for _, field := range st.Fields.List {
						if field.Tag == nil {
							continue
						}
						// 只看 json tag：响应模型的字段名以序列化名为准。
						name := strings.SplitN(strings.Trim(field.Tag.Value, "`"), ":", 2)
						if len(name) != 2 || !strings.HasPrefix(name[0], "json") {
							continue
						}
						jsonName := strings.Split(strings.Trim(name[1], `"`), ",")[0]
						if reCredentialField.MatchString(jsonName) {
							t.Errorf("%s: %s 响应字段 %q 含凭证类词根（D9：凭证不经 API 回传）",
								filepath.Base(path), ts.Name.Name, jsonName)
						}
					}
				}
			}
			return nil
		})
		if err != nil {
			t.Fatalf("扫描 %s 失败: %v", dir, err)
		}
	}
}
