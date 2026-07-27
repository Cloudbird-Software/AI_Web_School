"""T-W2-011 门证书签发服务子包.

唯一对外导出：issue_certificate / CertificateIssuanceError。
具体实现在 service.py 中。
"""
from src.core.gate.certifier.service import (
    CertificateIssuanceError,
    issue_certificate,
)

__all__ = ["CertificateIssuanceError", "issue_certificate"]
