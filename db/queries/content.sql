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
