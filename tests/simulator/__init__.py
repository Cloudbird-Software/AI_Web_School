"""T-W4-043 学生模拟器（dev-only OpenAPI 参考客户端）.

定位：小程序团队的接口参考实现；模拟学生全部 C 端行为，其全部调用即
consumer-driven 契约测试主体（架构 §4.8 / OPC §6.5）.

宪法 A5/X6：本模拟器不 import 学科包/学段包；只通过 openapi-v1.yaml 定义的
C 端接口与平台交互。
"""
