"""W4-S7 合规层：PII 保险库 / 家长授权 / 跨用户排名禁令 / 姓名脱敏.

落地架构 v2 §4.8 合规层（由横切组件升格为设计约束）与宪法 D7/D8：
- D7 PII 隔离：学生直接标识只允许存在于独立 PII 保险库 schema；
  主库只有 student_alias_id；LLM/TTS 调用前必须剥离 PII。
- D8 不排名：代码层不得提供跨用户成绩排名的查询路径；对外呈现一律等级化。

子模块：
- pii_encryption：PII 保险库读写（AES-256-GCM 列级加密，应用层加解密，
  密钥环境变量注入，明文不落地磁盘）。
- parental_consent：家长授权记录（版本化/范围/时间/撤回，append-only）。
- redaction：扫描件姓名占位替换（纯字符串匹配，不做 OCR）。

宪法 A5/X6：本包不 import 任何学科包/学段包。
"""
from __future__ import annotations

__all__: list[str] = []
