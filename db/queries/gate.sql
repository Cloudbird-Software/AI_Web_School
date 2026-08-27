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

-- name: InsertGateFailure :exec
INSERT INTO gate_failure (
	failure_id, artifact_type, artifact_ref,
	validator_id, validator_version, policy_version,
	reason, evidence, failed_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
