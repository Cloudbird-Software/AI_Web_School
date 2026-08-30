-- T-W5-002（SQL-2）：校验门域的证书验真与失败留痕语句面。
-- 读取面：CertificateVerifier 按 cert_id 取证（存在性）并做绑定一致性比对
-- （cert_type / artifact_ref 与发布事务声明的用途一致，D2 发布强制三段之一：
-- 「持证且证是给这个产物的」）。写入面：FailureTrail 往 gate_failure 落一条
-- 失败事实（append-only 触发器物理兜底 UPDATE/DELETE 禁令）。
-- 事务纪律（D11）：两者都必须运行在调用方已 begin 的显式事务内
-- （core/gate.WithTx 绑定传入），提交/回滚由最外层调用方统一持有，
-- 本域永不自 commit——发布行、证书引用与失败留痕同进同退。

-- name: GetGateCertificate :one
SELECT * FROM gate_certificate WHERE cert_id = $1;

-- ── GO-RW-001：门证书只读查询面（GET /gate_certificates 的取证语句）─────────
-- 只读（宪法 D1 仅 SELECT）：证书 + 关联运行记录 + 判定明细的三段取证。
-- 判定明细经 gate_run 反查（证书 → 运行 → 判定）一次带全，避免按 run 逐个
-- N+1；排序键显式钉死（run_at+run_id / verdict_id），读面确定性不随执行计划漂移.

-- name: ListGateRunsByCertificate :many
-- 某证书关联的全部验证器运行记录（按运行时刻升序，run_id 决胜同刻并列）。
SELECT * FROM gate_run WHERE certificate_id = $1 ORDER BY run_at ASC, run_id ASC;

-- name: ListGateVerdictsByCertificate :many
-- 某证书下全部运行记录的判定明细（经 gate_run 归到同一证书，verdict_id 升序）。
SELECT v.verdict_id, v.run_id, v.detail, v.created_at
FROM gate_verdict AS v
JOIN gate_run AS r ON r.run_id = v.run_id
WHERE r.certificate_id = $1
ORDER BY v.verdict_id ASC;

-- name: InsertGateFailure :exec
INSERT INTO gate_failure (
	failure_id, artifact_type, artifact_ref,
	validator_id, validator_version, policy_version,
	reason, evidence, failed_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
