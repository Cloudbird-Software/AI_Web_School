-- T-W5-033（SQL-2）首批查询：内容版本账读路径（item_version / publication）。
-- 查询层随域扩展增量添加；写路径（append-only 账入账）在 W5-R 后续任务卡
--（T-W5-001/002/003）以显式事务语句进本目录，不在 Go 代码拼 SQL。

-- name: GetItemVersion :one
-- 单版本取回（证书验真 / 内容寻址的读侧基元）。
SELECT * FROM item_version WHERE item_version_id = $1;

-- name: ListItemVersionsByItem :many
-- 某母题实例的全部版本（按入账时间升序——账只增不改，时序即版本序）。
SELECT * FROM item_version WHERE item_id = $1 ORDER BY created_at ASC;

-- name: GetLatestPublication :one
-- 当前生效发布（发布服务 T-W5-003 证书验真的入口查询）。
SELECT * FROM publication WHERE item_id = $1 ORDER BY published_at DESC LIMIT 1;

-- ── GO-RW-001：内容资产只读查询面（GET /items /templates 的取证语句）────────
-- 只读（宪法 D1 仅 SELECT）：item / item_template 是指针表（非三本账），身份与
-- current_version_id 指针的读侧基元；指针指向的版本行由调用方再经 GetItemVersion
-- / GetItemTemplateVersion 取回（两步取证而非 JOIN——指针悬空要在应用层
-- fail-loud，JOIN 会把账面残缺静默折损成 NULL）。

-- name: GetItem :one
-- 单个 item 身份行（GET /items/{item_id} 的主取数）。
SELECT * FROM item WHERE item_id = $1;

-- name: GetItemTemplate :one
-- 单个母题身份行（GET /templates/{template_id} 的主取数）。
SELECT * FROM item_template WHERE template_id = $1;

-- name: GetItemTemplateVersion :one
-- 单个母题版本行（指针 current_version_id 的解引用读侧）。
SELECT * FROM item_template_version WHERE template_version_id = $1;

-- ── T-W5-003：发布事务写面（PublishService 专用）───────────────────────────
-- 事务纪律（D11）：以下语句全部运行在调用方已 begin 的显式事务内，提交/回滚由
-- 最外层调用方统一持有——状态前移、签发账与指针前移同进同退，本域不自 commit。
-- item_version 的 UPDATE 仅限契约 §4 受控状态机字段（status/gate_certificate_id/
-- published_at + rendered_snapshot 非空兜底）；内容六块永不 UPDATE（D1）——
-- 0024 未对 item_version 挂整表 append-only 触发器，正是为本次合法前移留的面。

-- name: UpdateItemVersionPublished :exec
-- 状态前移 draft/quarantined → published：写门证书与发布时刻。
-- rendered_snapshot 不做任何兜底补写（审计 #161）：假渲染快照进内容账违反
-- 「门不过不入库/不伪造数据」；缺失由 PublishService 取证面显式拒绝
-- （ErrRenderedSnapshotMissing，fail-loud），0002 非空 CHECK 兜底防线保留。
UPDATE item_version SET
	status = 'published',
	gate_certificate_id = $2,
	published_at = $3
WHERE item_version_id = $1;

-- name: InsertPublication :exec
-- 签发账入账：publication 行（发布事件本体；FK 一致性由 0002/0028 的
-- DEFERRABLE 外键在 COMMIT 边界统一验证，语句先后序自由）。
INSERT INTO publication (
	publication_id, item_id, item_version_id, gate_certificate_id,
	published_by, published_at
) VALUES ($1, $2, $3, $4, $5, $6);

-- name: ForwardItemCurrentVersion :exec
-- 前移 item.current_version_id 指针：0002 触发器只挂 AFTER INSERT，本路径以
-- UPDATE 前移状态机字段、触发器不触发，须由应用层显式前移（冻结
-- publication.py 同款动作；指针表不在三本账之列，UPDATE 不违 D1）。
UPDATE item SET current_version_id = $2 WHERE item_id = $1;

-- ── #156（内容入账链路）：item / item_version 写面（cmd/ingest 专用）────────
-- 事务纪律（D11 / 铁律 9）：全部语句运行在调用方已 begin 的同一显式事务内，
-- COMMIT/ROLLBACK 归最外层 cmd 持有；门不过即整体回滚，失败留痕走独立事务。
-- item / item_template / item_template_version 是身份/指针表（非三本账），
-- INSERT 不违 D1；item_version 是不可变内容快照账，本语句面只有 INSERT。

-- name: UpsertItemTemplate :exec
-- 母题身份行就位（fk_itv_template 的被引用面）。DO NOTHING：母题身份是内容
-- 寻址前的稳定 id，重放同批 JSONL 时身份行已存在即幂等跳过（不改既有行）。
INSERT INTO item_template (template_id, pack_id)
VALUES ($1, $2)
ON CONFLICT (template_id) DO NOTHING;

-- name: UpsertItemTemplateVersion :exec
-- 母题版本行就位（item.template_version_id 非延迟外键的被引用面——0002 直挂
-- fk_item_template_version，语句先后序必须先版本后实例）。template_version_id
-- = sha256(canonical(spec)) 内容寻址（packs mustTemplateVersionID 同口径），
-- 碰撞即同内容，DO NOTHING 幂等；状态 draft：入账链路不代行母题发布签发。
INSERT INTO item_template_version (template_version_id, template_id, dsl_version, spec, status)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (template_version_id) DO NOTHING;

-- name: InsertItem :exec
-- 不变身份行入账：A/B 级 item_id = item_version_id（自引用，冻结
-- instantiation engine.py 同款：item_id = item_version_id）。重放同批撞 PK
-- 即天然幂等屏障（账行身份由调用方定型，不 DO NOTHING——重复入账必须显形）。
INSERT INTO item (item_id, pack_id, tier, template_version_id)
VALUES ($1, $2, $3, $4);

-- name: InsertItemVersion :exec
-- 不可变内容快照 draft 入账：契约 §2.2 六块 JSONB + lineage 全写
-- （lineage 携带 template_id/source/operator/pack_id/corpus_version_id 与
-- pack_digest/engine_digest——发布事务重算公式一的证据链，审计卡 #156 点名）。
INSERT INTO item_version (
	item_version_id, item_id, status,
	objective, interaction_ref, content, scoring_ref, error_bindings, lineage
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
