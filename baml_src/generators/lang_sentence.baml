// W6 语文轮半确定档：句子重组操作员（issue #34 §六三档结构第二档）。
//
// 铁律同 operators.baml（宪法 A2/D10 + issue #34 D-A）：LLM 产出永远是 draft。
// Go 侧 packs/subjectlang.SentenceReorgSolvability 做代码验可解性（独立于 LLM
// 调用面），全过才成实例；任一检查失败即丢弃 draft（draft 永远是 draft）。
// 本文件是本母题 prompt 层唯一事实源；Go 侧经 core/ai Caller 注入出站执行面，
// 测试用 fake Caller，不要求真实 LLM 调用。

// SentenceReorg 是句子重组 draft 的输出契约（与
// packs/subjectlang.SentenceReorgDraft 的 JSON 字段一一对应）。
class SentenceReorg {
  sentence string @description(#"题干句子：围绕题材词写成、适合该学段的一句完整中文句子，句中恰好包含一次挖空词"#)
  answer string @description(#"挖空词：句中被挖去的目标词，必须来自入库语料词表（word_in_vocab 判定域）"#)
  distractors string[] @description(#"干扰词数组：恰好 3 个，全部在词表内、两两互异、都不等于挖空词、都不出现在题干句中"#)
  explanation string @description(#"解析：说明挖空词为何恰配该空格（非空，供评价者核查）"#)
}

// GenerateSentenceReorg 依据题材词与学段生成一道「选词填空」草稿。
// vocab_candidates 是入库语料词表全量（逗号分隔）——answer 与 distractors
// 的判定域：LLM 不知道词表内容时只能凭空造词，校验器必然拒绝（fail-closed
// 浪费一次出站）；把判定域显式递给出站面，候选即合法域。
function GenerateSentenceReorg(source_word: string, gradeband: string, vocab_candidates: string) -> SentenceReorg {
  client StepFun // StepFun OpenAI 兼容网关（clients.baml 注册；环境变量 STEPFUN_API_KEY）。Go 侧测试用 fake Caller，不要求真实调用。
  prompt #"
    你是小学语文练习题生成操作员（半确定档：句子重组）。依据题材词与学段，
    生成一道「选词填空」草稿：一句围绕题材词的完整句子 + 句中挖空词 +
    3 个干扰词 + 解析。

    规则（Go 侧校验器将逐条复核，任何一条不满足该草稿作废）：
    1. sentence：一句适合该学段学生的完整中文句子，围绕题材词展开。
       句子给出完整形式——挖空词原样出现在句中恰好一次；
       不要自己挖空、不要用括号/下划线/空格代替挖空词（挖空由系统完成）。
    2. answer（挖空词）：只能从下方词表（vocab_candidates）里选。
    3. distractors：恰好 3 个干扰词，全部从词表里选；两两互异；都不等于
       挖空词；都不出现在题干句中（避免多解）。
    4. explanation：非空，说明挖空词为何恰配该空格。

    题材词（source_word）：{{ source_word }}
    学段（gradeband）：{{ gradeband }}
    词表（vocab_candidates，answer 与 distractors 只能从这里选）：
    {{ vocab_candidates }}

    {{ ctx.output_format }}
  "#
}

test sentence_reorg_golden {
  functions [GenerateSentenceReorg]
  args {
    source_word "太阳"
    gradeband "L"
    vocab_candidates "太阳,月亮,星星,学校,老师,同学,朋友,上学,读书,写字,说话,听话,看见,听到,走路,跑步,游泳,吃饭,喝水"
  }
}
