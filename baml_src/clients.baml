// 客户端注册：StepFun（阶跃星辰）OpenAI 兼容网关。
//
// 接入面（平台文档 step_plan 推理接口）：BaseURL
// https://api.stepfun.com/step_plan/v1，Chat Completions 与 OpenAI 协议
// 逐字段兼容——BAML openai provider 直接可用。凭据零明文入仓（治理 AR-3）：
// api key 经环境变量 STEPFUN_API_KEY 注入，进程环境透传见
// baml_client/runtime.go getEnvVars。
//
// 模型档位注册化（宪法 A7/D10）：provider/model/model_version 三要素最终
// 全进 AI 台账——cmd/langgen -provider/-model/-model-version 旗标与本 client
// 的 options.model 必须同值（D10 台账五要素对齐，见 langgen runReorg 装配链）。
client StepFun {
  provider openai
  options {
    model "step-3.7-flash"
    base_url "https://api.stepfun.com/step_plan/v1"
    api_key env.STEPFUN_API_KEY
  }
}
