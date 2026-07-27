"""教研工作台（T-W2-041/042/043）.

FastAPI + Jinja2 服务端渲染 HTML；与 src/api 共存，独立挂载。
- 登录：单用户静态 token（环境变量 WORKBENCH_TOKEN），W2 不做完整 RBAC。
- 题库只读列表/详情（T-W2-041）
- 母题表单 + 按轴抽样预览（T-W2-042）
- 签发闭环（T-W2-043）

宪法 A5/X6：本包不 import 任何学科包；调用 src/core 服务层。
宪法 D1：写入路径走 src/core/content/writer.py + publication.py，
工作台不绕过门强制直写 item_version。
"""
