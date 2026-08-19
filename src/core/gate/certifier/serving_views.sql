-- T-W2-011 serving 区只读视图与角色定义（契约冻结文本）
-- =============================================================================
-- 来源：specs/contracts/db/item-model.md#4 §4 规则 3
--   authoring / serving 逻辑分区：组装服务只读 serving 视图
--   （status='published' 且未退役且素材许可未过期）。
--
-- 本文件是 serving 区访问控制的契约冻结文本——人类逐行审查批准后转 frozen。
-- Alembic 迁移 0006_serving_views.py 把本文件 SQL 应用到数据库。
--
-- 防护层次（D2 物理强制，三层共同防护）：
--   1. CHECK 约束（ck_iv_published_requires_gate_cert 等）：
--      published_at 非空必伴随 gate_certificate_id 非空。
--   2. append-only 触发器（trg_*_append_only）：
--      三本账禁止 UPDATE/DELETE。
--   3. 角色权限（本文件落地）：serving_reader 无底层表 INSERT/UPDATE/DELETE
--      权限——绕过写入服务直写 serving 表在 DB 层失败（验收标准 #2/#3）。
--
-- 退役是状态不是删除（§4 规则 4）：retired 状态的版本不出现在 serving 视图，
-- 但历史作答/历史试卷中的引用永久有效（R-Q-26）。
-- =============================================================================


-- ────────────────────────────────────────────────────────────────────
-- 1. serving_reader 角色（低权限，只读 serving 视图）
-- ────────────────────────────────────────────────────────────────────
-- NOLOGIN 组角色（#43 Security 修复）：固定口令不再进代码库与迁移记录。
-- 测试/部署侧按需创建自己的 LOGIN 角色并 GRANT serving_reader（继承只读
-- 视图权限）；gate-bypass 实证（验收 #3）由测试 fixture 动态建临时登录角色。

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'serving_reader') THEN
        CREATE ROLE serving_reader NOLOGIN;
    END IF;
END $$;


-- ────────────────────────────────────────────────────────────────────
-- 2. serving 视图：item_version published + 未退役
-- ────────────────────────────────────────────────────────────────────
-- 组装服务查询题目版本的唯一入口——只暴露 published 且 retired_at 为空的版本。
-- retired_at IS NULL 是 defense-in-depth：status='retired' 已被 status='published'
-- 排除，但 retired_at 列字段作为审计时间戳，仍可作附加过滤层。

CREATE OR REPLACE VIEW v_serving_item_version AS
SELECT
    iv.item_version_id,
    iv.item_id,
    iv.status,
    iv.objective,
    iv.interaction_ref,
    iv.content,
    iv.scoring_ref,
    iv.error_bindings,
    iv.lineage,
    iv.rendered_snapshot,
    iv.gate_certificate_id,
    iv.published_at,
    iv.retired_at,
    iv.created_at,
    i.pack_id,
    i.tier,
    i.template_version_id
FROM item_version iv
JOIN item i ON i.item_id = iv.item_id
WHERE iv.status = 'published'
  AND iv.published_at IS NOT NULL
  AND iv.retired_at IS NULL;


-- ────────────────────────────────────────────────────────────────────
-- 3. serving 视图：material_version published + 许可未过期
-- ────────────────────────────────────────────────────────────────────
-- 过期许可素材不得用于新组卷（§4 serving 规则）。
-- decision IS NULL OR decision='approved'：兼容历史数据（license 无 decision 时
-- 默认放行；新数据必须显式 approved）。

CREATE OR REPLACE VIEW v_serving_material_version AS
SELECT
    mv.material_version_id,
    mv.material_id,
    mv.content_ref,
    mv.license_id,
    mv.status,
    mv.lineage,
    mv.gate_certificate_id,
    mv.published_at,
    mv.retired_at,
    mv.created_at,
    m.kind,
    m.pack_id,
    ml.source AS license_source,
    ml.rights_holder,
    ml.scope AS license_scope,
    ml.expires_at AS license_expires_at,
    ml.decision AS license_decision
FROM material_version mv
JOIN material m ON m.material_id = mv.material_id
LEFT JOIN material_license ml ON ml.license_id = mv.license_id
WHERE mv.status = 'published'
  AND mv.published_at IS NOT NULL
  AND mv.retired_at IS NULL
  AND (ml.expires_at IS NULL OR ml.expires_at > now())
  AND (ml.decision IS NULL OR ml.decision = 'approved');


-- ────────────────────────────────────────────────────────────────────
-- 4. serving 视图：corpus_version published + 未退役
-- ────────────────────────────────────────────────────────────────────
-- 语料库版本与 item_version / material_version 同构：状态机过滤 + retired_at 兜底。
-- 许可未过期过滤复用 material_license 表（corpus_version.license_id FK→material_license）。

CREATE OR REPLACE VIEW v_serving_corpus_version AS
SELECT
    cv.version_id,
    cv.asset_id,
    cv.content_ref,
    cv.license_id,
    cv.status,
    cv.lineage,
    cv.gate_certificate_id,
    cv.published_at,
    cv.retired_at,
    cv.created_at,
    ca.kind,
    ca.pack_id,
    ml.source AS license_source,
    ml.expires_at AS license_expires_at,
    ml.decision AS license_decision
FROM corpus_version cv
JOIN corpus_asset ca ON ca.asset_id = cv.asset_id
LEFT JOIN material_license ml ON ml.license_id = cv.license_id
WHERE cv.status = 'published'
  AND cv.published_at IS NOT NULL
  AND cv.retired_at IS NULL
  AND (ml.expires_at IS NULL OR ml.expires_at > now())
  AND (ml.decision IS NULL OR ml.decision = 'approved');


-- ────────────────────────────────────────────────────────────────────
-- 5. 授权：serving_reader 只能 SELECT 视图，无任何底层表权限
-- ────────────────────────────────────────────────────────────────────
-- 关键防护：未对 item_version / material_version / corpus_version 等底层表
-- GRANT 任何权限——serving_reader 直写底层表会在 DB 层报「permission denied」。
-- 这是「直写 serving 区失败」的角色层兜底（验收 #2/#3）。

GRANT SELECT ON v_serving_item_version TO serving_reader;
GRANT SELECT ON v_serving_material_version TO serving_reader;
GRANT SELECT ON v_serving_corpus_version TO serving_reader;
