"""
IB Log Translator — replaces Chinese IB API messages with English equivalents.

IB TWS set to Chinese language produces Unicode-escaped Chinese text in all
warning/error log messages. This module installs a logging.Filter on the
ib_insync loggers that detects the error code and substitutes the standard
English message.

Usage (called once at startup):
    from ib_log_translator import install
    install()
"""
import logging
import re

# ── IB Error Code → English Message ──────────────────────────────────────────
# Source: IB API documentation + TWS error reference
IB_MESSAGES = {
    # ── Connectivity ──────────────────────────────────────────────────────────
    1100: "Connectivity between IB and Trader Workstation has been lost.",
    1101: "Connectivity between IB and TWS has been restored - data maintained.",
    1102: "Connectivity between IB and TWS has been restored - data lost.",
    1300: "TWS socket port has been reset and this connection is being dropped.",
    2100: "API client has been unsubscribed from account data.",
    2101: "Requested market data is not subscribed. Displaying delayed market data.",
    2102: "Market data farm connection is restored.",
    2103: "Market data farm connection is broken.",
    2104: "Market data farm connection is OK.",
    2105: "HMDS data farm connection is broken.",
    2106: "HMDS data farm connection is OK.",
    2107: "HMDS data farm connection is inactive but available on demand.",
    2108: "Market data farm connection is inactive but available on demand.",
    2109: 'Order Event Warning: "Outside Regular Trading Hours" attribute ignored based on order type/destination.',
    2110: "Connectivity between Trader Workstation and server is broken.",
    2119: "Market data farm is connecting.",
    2137: "Cross Side Warning.",
    2148: "Sec-def data farm connection is OK.",
    2158: "Sec-def data farm connection is OK.",
    2176: "Market data farm connection is OK.",

    # ── Order / Account ───────────────────────────────────────────────────────
    202:  "Order Cancelled - reason: ",
    321:  "Server error when reading an API client request.",
    354:  "Requested market data is not subscribed.",
    366:  "No historical data query found for ticker id.",
    382:  "No scanner subscription found for ticker id.",
    399:  "Order message error.",
    404:  "Clearing away PnL data.",
    430:  "The order size cannot be zero.",
    434:  "The order quantity cannot be fractional.",
    10000: "Cross currency combo error.",
    10089: "Requested market data requires additional subscription.",
    10167: "Requested market data is not subscribed. Delayed market data is available.",
    10197: "No market data during competing live session.",
    10225: "Bust event occurred, current subscription is deactivated.",

    # ── Historical Data ───────────────────────────────────────────────────────
    162:  "Historical market data Service error message.",
    165:  "Historical market data Service query message.",
    200:  "No security definition has been found for the request.",
    300:  "Can't find EId with ticker Id.",
    301:  "Invalid ticker action.",
    302:  "Error in market rule request.",
    309:  "Only one type of combo legs can be requested per order.",
    310:  "Only one type of combo legs can be submitted per order.",
    311:  "Only one type of combo legs can be submitted per order.",

    # ── Connection / Auth ──────────────────────────────────────────────────────
    501:  "Already connected.",
    502:  "Couldn't connect to TWS. Confirm that 'Enable ActiveX and Socket Clients' is enabled.",
    503:  "The TWS is out of date and must be upgraded.",
    504:  "Not connected.",
    505:  "Fatal Error: Unknown message id.",
    506:  "Unsupported Version.",
    507:  "Bad Message Length.",
    508:  "Bad Message.",
    509:  "Failed to send message.",
    510:  "Request Market Data Sending Error - ",
    511:  "Cancel Market Data Sending Error - ",
    512:  "Order Sending Error - ",
    513:  "Account Update Request Sending Error -",
    514:  "Request For Open Orders Sending Error -",
    515:  "Server Request Sending Error -",
    516:  "API client error",
    517:  "Server Validation Error",
}

# Regex to extract code from ib_insync log messages like:
#   "Warning 2108, reqId -1: <text>:<farm>"
#   "Error 200, reqId 1: <text>"
#   "API connection failed: ..."
_CODE_RE = re.compile(r'\b(?:Warning|Error)\s+(\d+)\s*,', re.IGNORECASE)
_FARM_RE = re.compile(r':(\w+)$')   # farm name suffix like ":usfarm", ":jfarm"


class _IBTranslateFilter(logging.Filter):
    """
    Logging filter attached to ib_insync.* loggers.
    Replaces non-ASCII (Chinese) message text with the English equivalent
    looked up by the IB error code embedded in the message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()

        # Only process if the message contains non-ASCII (Chinese) characters
        if msg.isascii():
            return True

        code_match = _CODE_RE.search(msg)
        if code_match:
            code = int(code_match.group(1))
            english = IB_MESSAGES.get(code)
            if english:
                # Extract farm name suffix if present
                farm_match = _FARM_RE.search(msg)
                farm = f" [{farm_match.group(1)}]" if farm_match else ""

                # Rebuild message: keep the Warning/Error prefix, replace body
                prefix_end = code_match.end()
                req_part   = msg[prefix_end:].split(":", 1)[0]  # ", reqId -1"
                record.msg  = f"Warning {code}{req_part}: {english}{farm}"
                record.args = ()
                return True

        # Fallback: just strip non-ASCII so it at least doesn't show escapes
        record.msg  = msg.encode("ascii", errors="replace").decode("ascii")
        record.args = ()
        return True


def install():
    """
    Install the translation filter on all ib_insync loggers.
    Call once at application startup (before IB connects).
    """
    f = _IBTranslateFilter()
    for name in ("ib_insync", "ib_insync.wrapper", "ib_insync.client",
                 "ib_insync.ib", "ib_insync.util"):
        logging.getLogger(name).addFilter(f)


# Auto-install when imported
install()
