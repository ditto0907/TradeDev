"""
IB Log Decoder — decodes literal \\uXXXX escape sequences in IB API log messages.

IB TWS (when set to Chinese) sends log messages as ASCII strings containing
literal \\uXXXX sequences, e.g.:
    Warning 2108, reqId -1: \\u5e02\\u573a...\\u3002:jfarm

This module installs a logging.Filter on all ib_insync.* loggers that detects
and decodes those sequences so the message displays correctly:
    Warning 2108, reqId -1: 市场数据场连接暂未激活，但可按需提供。:jfarm

Auto-installs on import. No translation — just proper Unicode decoding.
"""
import logging
import re

_UESC_RE = re.compile(r'\\u[0-9a-fA-F]{4}')


class _IBDecodeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if _UESC_RE.search(msg):
                record.msg  = msg.encode('raw_unicode_escape').decode('unicode_escape')
                record.args = ()
        except Exception:
            pass
        return True


def install():
    f = _IBDecodeFilter()
    for name in ("ib_insync", "ib_insync.wrapper", "ib_insync.client",
                 "ib_insync.ib", "ib_insync.util"):
        logging.getLogger(name).addFilter(f)


install()
