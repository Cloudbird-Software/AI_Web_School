// W5-R 操作员（Operator）最小函数（T-W5-030 spike）。
//
// 铁律（宪法 A2/D10 + issue #34 D-A）：LLM 产出永远是 draft，只有过校验门、
// 经写入服务入账的才是内容版本；内容源只来自入库语料（corpus_version 账），
// LLM 出题不造内容源——结构性杜绝幻觉进题库。
//
// 本文件是 prompt 层唯一事实源（languages.yaml llm_prompt 层：allowed=baml，
// 禁止 prompt 字符串插值进代码）。变更必须过 golden 快照（make baml-golden-check）。

// 生产线四级（宪法 A7：A/B/C/D 四级输出统一 ItemVersion）。
enum InteractionTier {
  A @description(#"结构化母题级（可无限实例化的函数库模板）"#)
  B @description(#"语料驱动变体级"#)
  C @description(#"人工/LLM 单件级草稿"#)
  D @description(#"外采入库级"#)
}

// Defining a data model.
class DraftInstance {
  stem string @description(#"题干（只能使用语料摘录中出现过的实体与措辞）"#)
  answer string @description(#"参考答案"#)
  tier InteractionTier @description(#"该草稿所属生产线档位"#)
  source_trace string @description(#"生成路径说明：母题签名与语料摘录如何进入题干（供评价者核查事实一致性）"#)
}

// Create a function to generate a candidate draft instance.
function GenerateDraftInstance(template_signature: string, corpus_excerpt: string) -> DraftInstance {
  // 模型档位注册化：供应商/模型与采样参数最终全进 AI 台账（D10）。
  client "openai/gpt-4o-mini" // 环境变量 OPENAI_API_KEY；spike 不做真实调用。
  prompt #"
    你是小学练习题生成操作员。依据母题结构签名与入库语料摘录，生成一个候选实例草稿。

    规则：
    1. 题干只能使用语料摘录中出现过的实体与措辞，不得引入语料之外的事实。
    2. 保持母题结构签名声明的题型结构与参数槽。
    3. source_trace 必须指明题干的每一处实体来自语料摘录的哪一句。

    母题结构签名：
    {{ template_signature }}

    入库语料摘录：
    {{ corpus_excerpt }}

    {{ ctx.output_format }}
  "#
}

test spike_golden_generate_draft {
  functions [GenerateDraftInstance]
  args {
    template_signature "{{\"interaction\":\"single_choice\",\"slots\":[\"a\",\"b\"]}}"
    corpus_excerpt #"
      圆是到定点距离等于定长的点的集合。半径为 3 厘米的圆，直径是 6 厘米。
    "#
  }
}

// verify redline baml change (BAML-1 应红)
