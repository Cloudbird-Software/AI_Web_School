// Go 生成器：baml_src → ../baml_client（生成物提交入库，纳入 go build/vet）。
// 生成命令（版本锁定，防 codegen 漂移）：
//   npx -y @boundaryml/baml@0.226.1 generate
generator target {
  output_type "go"
  output_dir "../"
  version "0.226.1"
  // go.mod module 名（生成内部 import 路径用）
  client_package_name "github.com/Cloudbird-Software/AI_Web_School"
  // 生成后立即格式化（GO-1：生成物同样必须 gofmt 零 diff）
  on_generate "gofmt -w ."
}
